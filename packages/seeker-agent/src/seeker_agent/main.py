"""Seeker Agent entry point.

One Python process, one CLI invocation per cron tick. No in-process scheduler:
cron calls us twice per arm (Moltbook + gonzo), 15 minutes apart, every 4h.
Crash recovery is free — the OS just doesn't run a tick that fails to start,
and watermarks only advance after a clean pass.

S295 ships the dry-run orchestrator: the loop walks the verb list, the gate
makes its decisions, audit events fire, but the venue clients and DB writes
raise :class:`NotImplementedError`. S296 fills them in.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import contextmanager
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
    """File-based lock per arm. Fail fast on contention; cron retries are cheap."""
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
# Per-post handler — runs gate decisions in order                             #
# --------------------------------------------------------------------------- #


def _process_classified_post(
    classification: Classification,
    post: PostRecord,
    settings: Settings,
    cards_seen: set[str],
) -> dict:
    """Walk the gates for one classification. Returns an audit-event dict.

    Returns the audit event the orchestrator should emit. Does not execute
    verbs that aren't implemented yet (cards / handshake) — instead records
    the would-act intent so dry-run output is useful.
    """
    base_event = {
        "event": "post_classified",
        "venue": post.venue,
        "post_id": post.post_id,
        "is_job_shaped": classification.is_job_shaped,
        "relevance": classification.relevance,
        "model": classification.model,
        "prompt_version": classification.prompt_version,
        "latency_ms": classification.latency_ms,
    }

    # Gate 1: relevance threshold
    decision = gate.check_relevance(classification, settings.seeker_relevance_threshold)
    if not decision.allowed:
        return {**base_event, "outcome": "dropped_at_gate", "gate": decision.gate, "reason": decision.reason}

    # If we got here, the classification matters. From here on, only the
    # handshake path is interesting; the gonzo arm short-circuits because
    # there are no cards on those venues.
    if post.venue.startswith("gonzo_"):
        return {**base_event, "outcome": "measured_only", "note": "gonzo arm; no handshake"}

    if not classification.has_vacancy_card_url:
        return {**base_event, "outcome": "no_card_url", "note": "no handshake path"}

    # Gate 2: URL allowlist
    decision = gate.check_card_url(
        classification.vacancy_card_url, settings.card_url_allowlist_regex
    )
    if not decision.allowed:
        return {**base_event, "outcome": "dropped_at_gate", "gate": decision.gate, "reason": decision.reason}

    assert classification.vacancy_card_url is not None  # gate would have denied None

    # Gate 3: dedup
    decision = gate.check_card_not_seen(classification.vacancy_card_url, cards_seen)
    if not decision.allowed:
        return {**base_event, "outcome": "dropped_at_gate", "gate": decision.gate, "reason": decision.reason}

    # In S295 we stop here — read_vacancy_card + check_card_schema_valid +
    # initiate_handshake all raise NotImplementedError. Surface as a planned
    # action in the audit so dry-run output is useful.
    return {
        **base_event,
        "outcome": "would_handshake",
        "card_url": classification.vacancy_card_url,
        "note": "S296: card fetch + schema validate + handshake initiation",
    }


# --------------------------------------------------------------------------- #
# Provider factory                                                            #
# --------------------------------------------------------------------------- #


def _build_provider(settings: Settings, force_echo: bool) -> ClassifierProvider:
    """Pick a classifier provider.

    For S295 we always return :class:`EchoProvider`. S296 wires Mistral and
    Cloudflare Workers AI; this factory is the choke point.
    """
    if force_echo or True:  # S295: always echo
        return EchoProvider(prompt_version=settings.classifier_prompt_version)
    # S296: branch on settings.seeker_llm_provider
    raise RuntimeError("unreachable in S295")


# --------------------------------------------------------------------------- #
# Single-tick driver                                                          #
# --------------------------------------------------------------------------- #


def run_tick(
    arm: ArmName,
    settings: Settings,
    posts: list[PostRecord],
    dry_run: bool,
    force_echo: bool = False,
) -> int:
    """Run one tick against an explicit batch of posts.

    `posts` is provided so this function is testable without venue clients.
    The real cron-driven path fetches posts via verbs.fetch_next_*(); S296.
    """
    # Kill switch
    decision = gate.check_kill_switch(settings.seeker_kill_file.exists())
    if not decision.allowed:
        audit.emit(
            {"event": "tick_aborted", "arm": arm, "reason": decision.reason},
            settings.experiment_db_url,
        )
        return 6

    if not dry_run:
        # Live mode requires credentials per arm. Fail closed.
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

    # cards_seen would be loaded from experiment_db in live mode; for dry-run
    # we start empty.
    cards_seen: set[str] = set()
    n_classified = 0
    n_dropped = 0

    try:
        with _arm_lock(arm, settings.tick_lock_dir):
            for post in posts:
                classification = verbs.classify_post(post, provider)
                # Validate the classifier output schema (defense in depth).
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
                    # In live mode we'd add to cards_seen after a successful
                    # handshake. Dry-run records the would-be add for transparency.
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


def cli() -> int:
    parser = argparse.ArgumentParser(
        prog="seeker-agent",
        description="Read-side reference agent for the Kitso Handshake protocol.",
    )
    parser.add_argument(
        "--arm",
        choices=("moltbook", "gonzo"),
        required=True,
        help="Which arm to run this tick.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Use EchoProvider, no DB writes.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    parser.add_argument(
        "--posts-file",
        type=Path,
        help=(
            "JSON file containing a list of PostRecord-shaped dicts. Required in "
            "S295 because venue fetchers are not yet implemented."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings()

    if args.posts_file is None:
        log.error(
            "S295 requires --posts-file. Venue fetchers (verbs 1 and 2) land in S296. "
            "Provide a JSON file with a list of post records to drive the orchestrator."
        )
        return 8

    import json

    try:
        raw = json.loads(args.posts_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("could not load posts file: %s", exc)
        return 9

    posts = [PostRecord(**r) for r in raw]
    return run_tick(arm=args.arm, settings=settings, posts=posts, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(cli())
