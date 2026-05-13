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
) -> dict:
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
    }

    decision = gate.check_relevance(classification, settings.seeker_relevance_threshold)
    if not decision.allowed:
        return {**base_event, "outcome": "dropped_at_gate", "gate": decision.gate, "reason": decision.reason}

    if post.venue.startswith("gonzo_"):
        return {**base_event, "outcome": "measured_only", "note": "gonzo arm; no handshake"}

    if not classification.has_vacancy_card_url:
        return {**base_event, "outcome": "no_card_url", "note": "no handshake path"}

    decision = gate.check_card_url(
        classification.vacancy_card_url, settings.card_url_allowlist_regex
    )
    if not decision.allowed:
        return {**base_event, "outcome": "dropped_at_gate", "gate": decision.gate, "reason": decision.reason}

    assert classification.vacancy_card_url is not None

    decision = gate.check_card_not_seen(classification.vacancy_card_url, cards_seen)
    if not decision.allowed:
        return {**base_event, "outcome": "dropped_at_gate", "gate": decision.gate, "reason": decision.reason}

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
    """Pick a classifier provider.

    Selection:
    - ``force_echo=True`` → EchoProvider (used by dry-run smoke tests)
    - ``seeker_llm_provider == "mistral"`` → MistralProvider (S296)
    - ``seeker_llm_provider == "cloudflare"`` → CloudflareProvider (S297 — not yet wired)
    """
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
) -> int:
    """Run one tick against an explicit batch of posts.

    ``posts`` is provided externally so this function is testable without
    venue clients. The CLI builds the batch (from --posts-file, --fetch-gonzo,
    or — in S297 — --fetch-moltbook) before calling this.
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
    log.info("provider=%s arm=%s posts=%d dry_run=%s", provider.name, arm, len(posts), dry_run)

    cards_seen: set[str] = set()
    n_classified = 0
    n_dropped = 0

    try:
        with _arm_lock(arm, settings.tick_lock_dir):
            for post in posts:
                try:
                    classification = verbs.classify_post(post, provider)
                except Exception as exc:
                    audit.emit(
                        {
                            "event": "classifier_call_failed",
                            "venue": post.venue,
                            "post_id": post.post_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                        settings.experiment_db_url,
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
                    continue

                event = _process_classified_post(classification, post, settings, cards_seen)
                audit.emit(event, settings.experiment_db_url)
                n_classified += 1
                if event.get("outcome") == "dropped_at_gate":
                    n_dropped += 1
                elif event.get("outcome") == "would_handshake":
                    cards_seen.add(event["card_url"])
    except RuntimeError as exc:
        audit.emit({"event": "tick_lock_contention", "arm": arm, "error": str(exc)}, settings.experiment_db_url)
        log.error("%s", exc)
        return 7

    audit.emit(
        {
            "event": "tick_complete",
            "arm": arm,
            "n_classified": n_classified,
            "n_dropped": n_dropped,
            "dry_run": dry_run,
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


def cli() -> int:
    parser = argparse.ArgumentParser(
        prog="seeker-agent",
        description="Read-side reference agent for the Kitso Handshake protocol.",
    )
    parser.add_argument("--arm", choices=("moltbook", "gonzo"), required=True)
    parser.add_argument("--dry-run", action="store_true", help="Skip live-mode checks; still calls the configured provider unless --force-echo.")
    parser.add_argument("--force-echo", action="store_true", help="Use EchoProvider regardless of SEEKER_LLM_PROVIDER (for testing the pipe).")
    parser.add_argument("--verbose", "-v", action="store_true")

    # Source: choose ONE
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--posts-file", type=Path, help="JSON file with list of PostRecord-shaped dicts.")
    src.add_argument("--fetch-gonzo", metavar="CHANNEL", help="Live-fetch a batch from sf4l_prod for the given gonzo channel.")

    parser.add_argument("--batch-size", type=int, default=20, help="Max posts per tick (default 20).")
    parser.add_argument("--since", help="Watermark: ISO-8601 datetime. Only posts after this are fetched.")

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

    # Source the batch
    if args.fetch_gonzo:
        if args.arm != "gonzo":
            log.error("--fetch-gonzo only makes sense with --arm gonzo")
            return 2
        if not settings.sf4l_prod_readonly_url:
            log.error(
                "no sf4l_prod_readonly_url in env or credentials file. "
                "Run with SF4L_PROD_READONLY_URL set or populate ~/.config/seeker/credentials.json"
            )
            return 4
        since = _parse_since(args.since)
        try:
            posts = verbs.fetch_next_gonzo_batch(
                channel=args.fetch_gonzo,
                since=since,
                limit=args.batch_size,
                sf4l_prod_readonly_url=settings.sf4l_prod_readonly_url,
            )
        except Exception as exc:
            log.error("fetch_next_gonzo_batch failed: %s", exc)
            audit.emit(
                {"event": "fetch_failed", "channel": args.fetch_gonzo, "error": f"{type(exc).__name__}: {exc}"},
                settings.experiment_db_url,
            )
            return 9
        log.info("fetched %d posts from %s", len(posts), args.fetch_gonzo)
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
    )


if __name__ == "__main__":
    sys.exit(cli())
