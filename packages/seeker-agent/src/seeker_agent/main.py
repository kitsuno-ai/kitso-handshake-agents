"""Seeker Agent entry point.

Subcommands:
- ``tick``    — run one tick (the original behaviour). Live or dry-run.
- ``halt``    — engage the kill switch (creates the kill file).
- ``resume``  — disengage the kill switch (deletes the kill file).
- ``status``  — print kill state, watermarks, and last-tick times per channel.

Backward note: in S295/S296 the CLI accepted ``--arm`` at the top level. As
of S298 the ``tick`` subcommand owns those flags. The cron wrapper invokes
``python3 -m seeker_agent.main tick ...``.
"""

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
    """Run one tick against an explicit batch of posts."""
    db = None  # S299: pre-bound; overwritten by the inner with-block
    decision = gate.check_kill_switch(settings.seeker_kill_file.exists())
    if not decision.allowed:
        audit.emit(
            {"event": "tick_aborted", "arm": arm, "reason": decision.reason},
            db,
        )
        return 6

    if not dry_run:
        missing = settings.check_live_mode(arm)
        if missing:
            audit.emit(
                {"event": "live_refused_missing_env", "arm": arm, "missing": missing},
            db,
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
            db,
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



                    # S299: capture rate-limit headers from this Mistral call (if any)
                    rl = getattr(provider, "last_rate_limit_observation", None)
                    if rl:
                        audit.emit(
                            {
                                "event": "rate_limit_observation",
                                "arm": arm,
                                "channel": post.submolt_or_channel,
                                "provider": provider.name,
                                **rl,
                            },
                            db,
                        )
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

                    classification_id = verbs.log_classification(classification, post, db)

                    event = _process_classified_post(
                        classification, post, settings, cards_seen, db, classification_id,
                    )
                    audit.emit(event, db)
                    n_classified += 1
                    if post.observed_at and (max_observed_at is None or post.observed_at > max_observed_at):
                        max_observed_at = post.observed_at
                    if event.get("outcome") == "dropped_at_gate":
                        n_dropped += 1
                    elif event.get("outcome") == "would_handshake":
                        cards_seen.add(event["card_url"])
                        db.record_card_seen(event["card_url"], handshake_initiated=False)

                if n_classified > 0 and channel and max_observed_at:
                    db.advance_watermark(arm, channel, max_observed_at)
                    log.info(
                        "advanced watermark arm=%s channel=%s to %s",
                        arm, channel, max_observed_at,
                    )

                # S299: emit tick_complete INSIDE the with-block so it lands in audit_events
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
                    db,
                )

    except RuntimeError as exc:
        audit.emit(
            {"event": "tick_lock_contention", "arm": arm, "error": str(exc)},
            None,
        )
        log.error("%s", exc)
        return 7

    return 0


# --------------------------------------------------------------------------- #
# Subcommand handlers                                                         #
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


def cmd_tick(args: argparse.Namespace) -> int:
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


def cmd_halt(args: argparse.Namespace) -> int:
    """Engage the kill switch — creates the kill file.

    The file content records who/when so 'status' can show context.
    Idempotent: re-running 'halt' refreshes the timestamp.
    """
    try:
        settings = Settings.load()
    except PermissionError as exc:
        log.error("seeker credentials file unusable: %s", exc)
        return 5

    kill_file = settings.seeker_kill_file
    payload = (
        f"halted_at={datetime.now(timezone.utc).isoformat()}\n"
        f"pid={os.getpid()}\n"
        f"user={os.environ.get('USER', os.environ.get('LOGNAME', 'unknown'))}\n"
    )
    if args.reason:
        payload += f"reason={args.reason}\n"

    kill_file.parent.mkdir(parents=True, exist_ok=True)
    kill_file.write_text(payload)
    print(f"kill switch engaged: {kill_file}", file=sys.stderr)
    print(payload, end="", file=sys.stderr)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Disengage the kill switch — deletes the kill file. Idempotent."""
    try:
        settings = Settings.load()
    except PermissionError as exc:
        log.error("seeker credentials file unusable: %s", exc)
        return 5

    kill_file = settings.seeker_kill_file
    if kill_file.exists():
        kill_file.unlink()
        print(f"kill switch disengaged: {kill_file} removed", file=sys.stderr)
    else:
        print(f"kill switch was not engaged ({kill_file} does not exist)", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print kill state + per-channel watermarks + last-tick times."""
    try:
        settings = Settings.load()
    except PermissionError as exc:
        log.error("seeker credentials file unusable: %s", exc)
        return 5

    kill_file = settings.seeker_kill_file
    print("=== Seeker Agent status ===")
    print(f"kill_file:           {kill_file}")
    if kill_file.exists():
        print("kill_switch_state:   ENGAGED")
        try:
            content = kill_file.read_text().strip()
            for line in content.splitlines():
                print(f"  {line}")
        except OSError:
            pass
    else:
        print("kill_switch_state:   disengaged")

    print(f"experiment_db:       {'configured' if settings.experiment_db_url else 'NOT configured'}")
    print(f"sf4l_prod_readonly:  {'configured' if settings.sf4l_prod_readonly_url else 'NOT configured'}")
    print(f"llm_provider:        {settings.seeker_llm_provider} (model={settings.mistral_model})")

    if settings.experiment_db_url:
        print()
        print("=== Watermarks ===")
        import psycopg2
        try:
            with psycopg2.connect(settings.experiment_db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT arm, channel, last_id_seen, last_tick_at "
                        "FROM watermarks "
                        "WHERE channel NOT LIKE 'itest-%' "
                        "ORDER BY arm, channel"
                    )
                    rows = cur.fetchall()
                    if not rows:
                        print("(none yet — first run hasn't completed)")
                    else:
                        print(f"{'arm':<8} {'channel':<32} {'last_observed_at':<32} {'last_tick_at'}")
                        for arm, channel, lis, lta in rows:
                            print(f"{arm:<8} {channel:<32} {str(lis):<32} {lta}")

                # Recent classifications count by venue
                print()
                print("=== Classifications by venue (last 7d) ===")
                cur = conn.cursor()
                cur.execute("""
                    SELECT venue, COUNT(*) AS n,
                           MIN(classified_at) AS first_at,
                           MAX(classified_at) AS last_at
                    FROM classifications
                    WHERE classified_at > NOW() - INTERVAL '7 days'
                    GROUP BY venue
                    ORDER BY n DESC
                """)
                rows = cur.fetchall()
                if not rows:
                    print("(no classifications in the last 7 days)")
                else:
                    print(f"{'venue':<32} {'n':>6}  first_at                       last_at")
                    for venue, n, first_at, last_at in rows:
                        print(f"{venue:<32} {n:>6}  {first_at}  {last_at}")
        except Exception as exc:
            print(f"could not query experiment_db: {type(exc).__name__}: {exc}")

    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Run a tick across all configured gonzo channels in a single process.

    Critical detail: the MistralProvider's pacing state lives on the instance.
    When the cron wrapper used to spawn one process per channel, each process
    got a fresh provider with min_gap=0, so the first call to a new channel
    fired immediately — bursting straight into Mistral's 50 req/min ceiling
    after the first channel's 20 posts had already consumed the budget.
    Running all channels in one process shares the provider and respects the
    free-tier sliding window.
    """
    db = None  # S299: pre-bound; overwritten by the inner with-block
    try:
        settings = Settings.load()
    except PermissionError as exc:
        log.error("seeker credentials file unusable: %s", exc)
        return 5

    if not settings.sf4l_prod_readonly_url:
        log.error("no sf4l_prod_readonly_url in env or credentials file")
        return 4

    channels = settings.gonzo_channel_list()
    if args.only:
        only = set(args.only.split(","))
        channels = [c for c in channels if c in only]
        if not channels:
            log.error("no channels matched --only %r", args.only)
            return 2

    # Build the provider ONCE so pacing state is shared
    try:
        provider = _build_provider(settings, force_echo=args.force_echo)
    except Exception as exc:
        log.error("provider build failed: %s", exc)
        return 11

    log.info(
        "sweep start channels=%d batch_size=%d provider=%s",
        len(channels), args.batch_size, provider.name,
    )

    overall_rc = 0
    for ch in channels:
        log.info("==> sweep channel=%s", ch)
        # Resolve since: --resume reads persisted watermark, else None
        since = None
        if args.resume:
            since = _resume_since_from_watermark(settings, "gonzo", ch)
            if since:
                log.info("    --resume: watermark resolved to %s", since.isoformat())

        try:
            posts = verbs.fetch_next_gonzo_batch(
                channel=ch,
                since=since,
                limit=args.batch_size,
                sf4l_prod_readonly_url=settings.sf4l_prod_readonly_url,
            )
        except Exception as exc:
            log.error("    fetch failed: %s", exc)
            audit.emit(
                {"event": "fetch_failed", "channel": ch, "error": f"{type(exc).__name__}: {exc}"},
                settings.experiment_db_url,
            )
            overall_rc = 9
            continue

        log.info("    fetched %d posts", len(posts))
        if not posts:
            audit.emit(
                {"event": "tick_complete", "arm": "gonzo", "channel": ch, "n_classified": 0, "n_dropped": 0, "dry_run": args.dry_run, "watermark_advanced_to": None},
            db,
            )
            continue

        rc = _run_tick_with_provider(
            arm="gonzo",
            settings=settings,
            posts=posts,
            provider=provider,
            dry_run=args.dry_run,
            channel=ch,
        )
        if rc != 0:
            overall_rc = rc

    log.info("sweep end rc=%d", overall_rc)
    return overall_rc


def _run_tick_with_provider(
    arm: ArmName,
    settings: Settings,
    posts: list[PostRecord],
    provider: ClassifierProvider,
    dry_run: bool,
    channel: str | None = None,
) -> int:
    """Like run_tick but accepts a pre-built provider (for sweep mode)."""
    decision = gate.check_kill_switch(settings.seeker_kill_file.exists())
    if not decision.allowed:
        audit.emit(
            {"event": "tick_aborted", "arm": arm, "reason": decision.reason},
            db,
        )
        return 6

    if not dry_run:
        missing = settings.check_live_mode(arm)
        if missing:
            audit.emit(
                {"event": "live_refused_missing_env", "arm": arm, "missing": missing},
            db,
            )
            log.error("live mode requires env vars: %s", ", ".join(missing))
            return 3

    cards_seen: set[str] = set()
    n_classified = 0
    n_dropped = 0
    max_observed_at: str | None = None

    try:
        with _arm_lock(arm, settings.tick_lock_dir):
            with ExperimentDB(settings.experiment_db_url) as db:
                for post in posts:
                    try:
                        classification = verbs.classify_post(post, provider)
                    except Exception as exc:
                        err_class = type(exc).__name__
                        audit.emit(
                            {"event": "classifier_call_failed", "venue": post.venue, "post_id": post.post_id, "error": f"{err_class}: {exc}"},
            db,
                        )
                        db.log_error(arm=arm, channel=post.submolt_or_channel, verb="classify_post", error_class=err_class, error_message=str(exc))
                        continue



                    # S299: capture rate-limit headers from this Mistral call (if any)
                    rl = getattr(provider, "last_rate_limit_observation", None)
                    if rl:
                        audit.emit(
                            {
                                "event": "rate_limit_observation",
                                "arm": arm,
                                "channel": post.submolt_or_channel,
                                "provider": provider.name,
                                **rl,
                            },
                            db,
                        )
                    try:
                        validate_payload(classification.to_dict())
                    except Exception as exc:
                        audit.emit(
                            {"event": "classifier_output_invalid", "venue": post.venue, "post_id": post.post_id, "error": str(exc)},
                            settings.experiment_db_url,
                        )
                        db.log_error(arm=arm, channel=post.submolt_or_channel, verb="classify_post", error_class="schema_invalid", error_message=str(exc))
                        continue

                    classification_id = verbs.log_classification(classification, post, db)
                    event = _process_classified_post(classification, post, settings, cards_seen, db, classification_id)
                    audit.emit(event, db)
                    n_classified += 1
                    if post.observed_at and (max_observed_at is None or post.observed_at > max_observed_at):
                        max_observed_at = post.observed_at
                    if event.get("outcome") == "dropped_at_gate":
                        n_dropped += 1
                    elif event.get("outcome") == "would_handshake":
                        cards_seen.add(event["card_url"])
                        db.record_card_seen(event["card_url"], handshake_initiated=False)

                if n_classified > 0 and channel and max_observed_at:
                    db.advance_watermark(arm, channel, max_observed_at)
                    log.info("    advanced watermark %s/%s to %s", arm, channel, max_observed_at)

                # S299: emit tick_complete INSIDE the with-block so it lands in audit_events
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
                    db,
                )

    except RuntimeError as exc:
        audit.emit({"event": "tick_lock_contention", "arm": arm, "error": str(exc)}, None)
        log.error("%s", exc)
        return 7

    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seeker-agent",
        description="Read-side reference agent for the Kitso Handshake protocol.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    # tick
    p_tick = sub.add_parser("tick", help="Run one classifier tick.")
    p_tick.add_argument("--arm", choices=("moltbook", "gonzo"), required=True)
    p_tick.add_argument("--dry-run", action="store_true")
    p_tick.add_argument("--force-echo", action="store_true")
    src = p_tick.add_mutually_exclusive_group()
    src.add_argument("--posts-file", type=Path)
    src.add_argument("--fetch-gonzo", metavar="CHANNEL")
    p_tick.add_argument("--batch-size", type=int, default=20)
    p_tick.add_argument("--since")
    p_tick.add_argument("--resume", action="store_true")
    p_tick.set_defaults(func=cmd_tick)

    # halt
    p_halt = sub.add_parser("halt", help="Engage the kill switch (stops future ticks).")
    p_halt.add_argument("--reason", help="Optional free-text reason; stored in the kill file for status.")
    p_halt.set_defaults(func=cmd_halt)

    # resume
    p_resume = sub.add_parser("resume", help="Disengage the kill switch.")
    p_resume.set_defaults(func=cmd_resume)

    # sweep
    p_sweep = sub.add_parser("sweep", help="Run a tick across all gonzo channels in one process (shared provider pacing).")
    p_sweep.add_argument("--dry-run", action="store_true")
    p_sweep.add_argument("--force-echo", action="store_true")
    p_sweep.add_argument("--batch-size", type=int, default=20)
    p_sweep.add_argument("--resume", action="store_true", default=True, help="Default ON for sweep (each channel resumes from its watermark).")
    p_sweep.add_argument("--no-resume", action="store_false", dest="resume")
    p_sweep.add_argument("--only", help="Comma-separated subset of gonzo channels to sweep.")
    p_sweep.set_defaults(func=cmd_sweep)

    # status
    p_status = sub.add_parser("status", help="Print kill state + watermarks.")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.cmd:
        parser.print_help()
        return 2

    return args.func(args)


if __name__ == "__main__":
    sys.exit(cli())
