"""Seeker Agent entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import audit, gate, verbs
from .classifier import (
    Classification,
    ClassifierProvider,
    EchoProvider,
    PostRecord,
    validate_payload,
)
from .config import ArmName, Settings
from .experiment_db import ExperimentDB

log = logging.getLogger("seeker_agent")


# --------------------------------------------------------------------------- #
# Lock                                                                        #
# --------------------------------------------------------------------------- #


@contextmanager
def _arm_lock(arm: ArmName, lock_dir: Path) -> Iterator[Path]:
    lock_path = lock_dir / f"seeker_{arm}.lock"
    if lock_path.exists():
        raise RuntimeError(f"another {arm} tick is running (lock: {lock_path})")
    lock_path.write_text(str(os.getpid()))
    try:
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# Per-post handler                                                            #
# --------------------------------------------------------------------------- #


def _process_classified_post(
    classification: Classification,
    post: PostRecord,
    settings: Settings,
    cards_seen: set[str],
    db: ExperimentDB,
    classification_id: int | None,
) -> dict:
    """Run the post-classify gates, persist the action row, return audit dict."""
    base_event = {
        "event": "post_classified",
        "venue": post.venue,
        "post_id": post.post_id,
        "is_job_shaped": classification.is_job_shaped,
        "relevance": classification.relevance,
        "model": classification.model,
        "prompt_version": classification.prompt_version,
        "latency_ms": classification.latency_ms,
        "extracted_role_title": classification.extracted_role_title,
        "extracted_company": classification.extracted_company,
        "spam_signals": classification.spam_signals,
        "classification_id": classification_id,
    }

    def _persist_action(verb_name: str, outcome: str, gate_name: str | None = None, **details):
        db.log_action(
            verb=verb_name,
            outcome=outcome,
            classification_id=classification_id,
            gate_name=gate_name,
            details=details or None,
        )

    decision = gate.check_relevance(classification, settings.seeker_relevance_threshold)
    if not decision.allowed:
        _persist_action("classify_post", "dropped_at_gate", gate_name=decision.gate, reason=decision.reason)
        return {**base_event, "outcome": "dropped_at_gate", "gate": decision.gate, "reason": decision.reason}

    if post.venue.startswith("gonzo_"):
        _persist_action("classify_post", "measured_only")
        return {**base_event, "outcome": "measured_only", "note": "gonzo arm; no handshake"}

    if not classification.has_vacancy_card_url:
        _persist_action("classify_post", "no_card_url")
        return {**base_event, "outcome": "no_card_url", "note": "no handshake path"}

    decision = gate.check_card_url(
        classification.vacancy_card_url, settings.card_url_allowlist_regex
    )
    if not decision.allowed:
        _persist_action("read_vacancy_card", "dropped_at_gate", gate_name=decision.gate, reason=decision.reason)
        return {**base_event, "outcome": "dropped_at_gate", "gate": decision.gate, "reason": decision.reason}

    assert classification.vacancy_card_url is not None

    decision = gate.check_card_not_seen(classification.vacancy_card_url, cards_seen)
    if not decision.allowed:
        _persist_action("initiate_handshake", "dropped_at_gate", gate_name=decision.gate, reason=decision.reason)
        return {**base_event, "outcome": "dropped_at_gate", "gate": decision.gate, "reason": decision.reason}

    _persist_action("initiate_handshake", "would_handshake", card_url=classification.vacancy_card_url)
    return {
        **base_event,
        "outcome": "would_handshake",
        "card_url": classification.vacancy_card_url,
        "note": "S297: card fetch + schema validate + handshake initiation",
    }


# --------------------------------------------------------------------------- #
# Provider factory                                                            #
# --------------------------------------------------------------------------- #


def _build_provider(settings: Settings, force_echo: bool = False) -> ClassifierProvider:
    if force_echo:
        return EchoProvider(prompt_version=settings.classifier_prompt_version)

    if settings.seeker_llm_provider == "mistral":
        from .providers.mistral import MistralProvider

        if not settings.mistral_api_key:
            raise RuntimeError(
                "SEEKER_LLM_PROVIDER=mistral but MISTRAL_API_KEY is unset"
            )
        return MistralProvider(
            api_key=settings.mistral_api_key,
            model=settings.mistral_model,
            api_base=settings.mistral_api_base,
            prompt_version=settings.classifier_prompt_version,
            temperature=settings.classifier_temperature,
            timeout_seconds=settings.classifier_timeout_seconds,
            max_post_chars=settings.classifier_max_post_chars,
            min_gap_seconds=settings.mistral_min_gap_seconds,
        )

    if settings.seeker_llm_provider == "cloudflare":
        raise NotImplementedError(
            "S297: Cloudflare Workers AI provider not yet wired. "
            "For now use SEEKER_LLM_PROVIDER=mistral or pass --force-echo."
        )

    raise RuntimeError(f"unknown provider: {settings.seeker_llm_provider}")


# --------------------------------------------------------------------------- #
# Tick driver                                                                 #
# --------------------------------------------------------------------------- #


def run_tick(
    arm: ArmName,
    settings: Settings,
    posts: list[PostRecord],
    dry_run: bool,
    force_echo: bool = False,
    channel: str | None = None,
) -> int:
    """Run one tick against an explicit batch of posts.

    ``posts`` is provided externally so this function is testable without
    venue clients. The CLI builds the batch (from --posts-file, --fetch-gonzo,
    or — in S297 — --fetch-moltbook) before calling this.

    If ``settings.experiment_db_url`` is set, the tick writes classification +
    action rows and advances the watermark on success. Otherwise it falls back
    to stderr-only auditing.
    """
    decision = gate.check_kill_switch(settings.seeker_kill_file.exists())
    if not decision.allowed:
        audit.emit(
            {"event": "tick_aborted", "arm": arm, "reason": decision.reason},
            settings.experiment_db_url,
        )
        return 6

    if not dry_run:
        missing = settings.check_live_mode(arm)
        if missing:
            audit.emit(
                {"event": "live_refused_missing_env", "arm": arm, "missing": missing},
                settings.experiment_db_url,
            )
            log.error("live mode requires env vars: %s", ", ".join(missing))
            return 3

    provider = _build_provider(settings, force_echo=force_echo)
    log.info(
        "provider=%s arm=%s posts=%d dry_run=%s persistence=%s",
        provider.name, arm, len(posts), dry_run, bool(settings.experiment_db_url),
    )

    cards_seen: set[str] = set()
    n_classified = 0
    n_dropped = 0
    max_observed_at: str | None = None

    try:
        with _arm_lock(arm, settings.tick_lock_dir):
            with ExperimentDB(settings.experiment_db_url) as db:
                for post in posts:
                    # Track the latest observed_at so we can advance the watermark
                    # at end of tick. ISO 8601 strings compare lexicographically when
                    # all are UTC-normalized, which we ensure in _row_to_post_record.
                    if max_observed_at is None or (post.observed_at and post.observed_at > max_observed_at):
                        max_observed_at = post.observed_at

                    try:
                        classification = verbs.classify_post(post, provider)
                    except Exception as exc:
                        err_class = type(exc).__name__
                        audit.emit(
                            {
                                "event": "classifier_call_failed",
                                "venue": post.venue,
                                "post_id": post.post_id,
                                "error": f"{err_class}: {exc}",
                            },
                            settings.experiment_db_url,
                        )
                        db.log_error(
                            arm=arm,
                            channel=post.submolt_or_channel,
                            verb="classify_post",
                            error_class=err_class,
                            error_message=str(exc),
                        )
                        log.warning(
                            "classifier failed on %s/%s: %s", post.venue, post.post_id, exc
                        )
                        continue

                    try:
                        validate_payload(classification.to_dict())
                    except Exception as exc:
                        audit.emit(
                            {
                                "event": "classifier_output_invalid",
                                "venue": post.venue,
                                "post_id": post.post_id,
                                "error": str(exc),
                            },
                            settings.experiment_db_url,
                        )
                        db.log_error(
                            arm=arm,
                            channel=post.submolt_or_channel,
                            verb="classify_post",
                            error_class="schema_invalid",
                            error_message=str(exc),
                        )
                        continue

                    # Persist the classification BEFORE running gates so the
                    # action row can FK to it.
                    classification_id = verbs.log_classification(classification, post, db)

                    event = _process_classified_post(
                        classification, post, settings, cards_seen, db, classification_id,
                    )
                    audit.emit(event, settings.experiment_db_url)
                    n_classified += 1
                    if event.get("outcome") == "dropped_at_gate":
                        n_dropped += 1
                    elif event.get("outcome") == "would_handshake":
                        cards_seen.add(event["card_url"])
                        db.record_card_seen(event["card_url"], handshake_initiated=False)

                # Advance watermark only when we processed at least one post
                if n_classified > 0 and channel and max_observed_at:
                    db.advance_watermark(arm, channel, max_observed_at)
                    log.info(
                        "advanced watermark arm=%s channel=%s to %s",
                        arm, channel, max_observed_at,
                    )
    except RuntimeError as exc:
        audit.emit(
            {"event": "tick_lock_contention", "arm": arm, "error": str(exc)},
            settings.experiment_db_url,
        )
        log.error("%s", exc)
        return 7

    audit.emit(
        {
            "event": "tick_complete",
            "arm": arm,
            "channel": channel,
            "n_classified": n_classified,
            "n_dropped": n_dropped,
            "dry_run": dry_run,
            "watermark_advanced_to": max_observed_at if n_classified > 0 and channel else None,
        },
        settings.experiment_db_url,
    )
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"--since must be ISO-8601 (got {raw!r}): {exc}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _resume_since_from_watermark(settings: Settings, arm: str, channel: str) -> datetime | None:
    """Read the persisted watermark and parse as datetime."""
    if not settings.experiment_db_url:
        return None
    with ExperimentDB(settings.experiment_db_url) as db:
        raw = db.get_watermark(arm, channel)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        log.warning("watermark for %s/%s is not ISO-8601: %r", arm, channel, raw)
        return None


def cli() -> int:
    parser = argparse.ArgumentParser(
        prog="seeker-agent",
        description="Read-side reference agent for the Kitso Handshake protocol.",
    )
    parser.add_argument("--arm", choices=("moltbook", "gonzo"), required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-echo", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")

    src = parser.add_mutually_exclusive_group()
    src.add_argument("--posts-file", type=Path)
    src.add_argument("--fetch-gonzo", metavar="CHANNEL")

    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--since", help="Watermark override: ISO-8601 datetime.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Use the persisted watermark from experiment_db as --since (overrides --since=auto).",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = Settings.load()
    except PermissionError as exc:
        log.error("seeker credentials file unusable: %s", exc)
        audit.emit({"event": "credentials_file_refused", "error": str(exc)}, None)
        return 5

    channel: str | None = None
    if args.fetch_gonzo:
        if args.arm != "gonzo":
            log.error("--fetch-gonzo only makes sense with --arm gonzo")
            return 2
        if not settings.sf4l_prod_readonly_url:
            log.error("no sf4l_prod_readonly_url in env or credentials file")
            return 4
        channel = args.fetch_gonzo

        # Pick the since: explicit --since > --resume from DB > None
        if args.resume:
            since = _resume_since_from_watermark(settings, args.arm, channel)
            if since:
                log.info("--resume: watermark resolved to %s", since.isoformat())
        else:
            since = _parse_since(args.since)

        try:
            posts = verbs.fetch_next_gonzo_batch(
                channel=channel,
                since=since,
                limit=args.batch_size,
                sf4l_prod_readonly_url=settings.sf4l_prod_readonly_url,
            )
        except Exception as exc:
            log.error("fetch_next_gonzo_batch failed: %s", exc)
            audit.emit(
                {"event": "fetch_failed", "channel": channel, "error": f"{type(exc).__name__}: {exc}"},
                settings.experiment_db_url,
            )
            return 9
        log.info("fetched %d posts from %s (since=%s)", len(posts), channel, since)
    elif args.posts_file:
        import json
        try:
            raw = json.loads(args.posts_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.error("could not load posts file: %s", exc)
            return 9
        posts = [PostRecord(**r) for r in raw]
    else:
        log.error("provide one of --posts-file or --fetch-gonzo")
        return 8

    return run_tick(
        arm=args.arm,
        settings=settings,
        posts=posts,
        dry_run=args.dry_run,
        force_echo=args.force_echo,
        channel=channel,
    )


if __name__ == "__main__":
    sys.exit(cli())
