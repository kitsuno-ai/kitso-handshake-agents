"""Vacancy Agent entry point.

The agent has one verb: post a vacancy card to a venue. It validates the card,
formats a venue-appropriate post body, posts once, logs the action, and exits.

There is intentionally no LLM in this code path. There is no loop, no retry
queue, no state. If you need any of those, build a layer above this — don't
embed it inside.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .cards import extract_post_summary, validate_card
from .config import Settings
from .moltbook_client import MoltbookClient, PostResult

log = logging.getLogger("vacancy_agent")


def _format_post_body(card: dict, card_url: str | None, jd_url: str | None) -> tuple[str, str]:
    """Build the (title, content) tuple for the venue post.

    Title is a short human-readable line. Content embeds card_url (for agents)
    and jd_url (for humans). Honest, machine-readable, no marketing.
    """
    s = extract_post_summary(card)
    title_parts = [s["role_title"]]
    if s["hiring_entity"]:
        title_parts.append(f"@ {s['hiring_entity']}")
    title_parts.append(f"({s['employment_type']}, {s['remote_policy'] or s['country']})")
    title = " ".join(title_parts)[:120]

    lines = [
        f"**{s['role_title']}** — {s['employment_type']}",
        "",
        f"- Hiring entity: {s['hiring_entity'] or 'see card'}",
        f"- Role family: {s['role_family']}",
        f"- Seniority: {s['seniority'] or 'see card'}",
        f"- Location: {s['country']} / {s['remote_policy'] or 'see card'}",
        "",
    ]
    if jd_url:
        lines.append(f"Human-readable JD: {jd_url}")
    if card_url:
        lines.append(f"Kitso Handshake v0.1 card: {card_url}")
    lines.append("")
    lines.append(
        "Posted via a Kitso Handshake reference agent "
        "(github.com/kitsuno-ai/kitso-handshake-agents). "
        "This post is part of a 21-day field study on agent-mediated hiring "
        "discovery; the role and the hiring intent are real."
    )

    return title, "\n".join(lines)


def _audit(event: dict, audit_db_url: str | None) -> None:
    """Append an audit record. Stdout if no DB configured.

    The audit shape is deliberately simple: a flat dict of strings + timestamps.
    """
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    if audit_db_url:
        # TODO(s293): wire to experiment_db (isolated Postgres)
        log.info("AUDIT-DB-PENDING %s", json.dumps(event))
    else:
        print("AUDIT " + json.dumps(event), file=sys.stderr)


def _slug_from_card_path(card_path: Path) -> str:
    """Derive a slug from the filename, used for default card URL."""
    return card_path.stem


def post_vacancy(
    card_path: Path,
    submolt: str,
    settings: Settings,
    dry_run: bool,
) -> int:
    """Post a single vacancy. Returns process exit code."""

    # 1. Validate
    log.info("validating card: %s", card_path)
    result = validate_card(card_path)
    if not result.ok:
        log.error("card validation FAILED")
        for err in result.errors:
            log.error("  %s", err)
        _audit(
            {"event": "validation_failed", "card_path": str(card_path), "errors": result.errors},
            settings.audit_db_url,
        )
        return 2

    assert result.card is not None  # ok=True implies card parsed
    card = result.card
    log.info("card validation OK")

    # 2. Build post
    slug = _slug_from_card_path(card_path)
    card_url = f"{str(settings.card_host_base).rstrip('/')}/{slug}.json"
    jd_url = f"{str(settings.jd_host_base).rstrip('/')}/{slug}"
    title, content = _format_post_body(card, card_url=card_url, jd_url=jd_url)
    log.info("post built: title=%r len(content)=%d", title, len(content))

    # 3. Dry-run short-circuit
    if dry_run:
        print("=" * 72, file=sys.stderr)
        print("DRY RUN — would post:", file=sys.stderr)
        print(f"  submolt: {submolt}", file=sys.stderr)
        print(f"  title:   {title}", file=sys.stderr)
        print("  content:", file=sys.stderr)
        for line in content.splitlines():
            print(f"    {line}", file=sys.stderr)
        print("=" * 72, file=sys.stderr)
        _audit(
            {"event": "dry_run", "card_path": str(card_path), "submolt": submolt, "title": title},
            settings.audit_db_url,
        )
        return 0

    # 4. Live mode — require credentials and a kill token
    missing = settings.check_live_mode()
    if missing:
        log.error("live mode requires env vars: %s", ", ".join(missing))
        _audit({"event": "live_refused_missing_creds", "missing": missing}, settings.audit_db_url)
        return 3

    # 5. Post
    client = MoltbookClient(
        api_key=settings.moltbook_api_key,  # type: ignore[arg-type]
        api_base=str(settings.moltbook_api_base),
        rate_limit_seconds=settings.rate_limit_seconds_between_posts,
    )
    pr: PostResult = client.post(submolt=submolt, title=title, content=content)
    _audit(
        {
            "event": "post_attempted",
            "card_path": str(card_path),
            "submolt": submolt,
            "ok": pr.ok,
            "status_code": pr.status_code,
            "post_id": pr.post_id,
            "error": pr.error,
        },
        settings.audit_db_url,
    )

    if pr.ok:
        log.info("post OK: id=%s", pr.post_id)
        return 0
    log.error("post FAILED: status=%s error=%s", pr.status_code, pr.error)
    return 4


def cli() -> int:
    parser = argparse.ArgumentParser(
        prog="vacancy-agent",
        description="Deterministic write-only Vacancy Agent (Kitso Handshake v0.1).",
    )
    parser.add_argument("--card", required=True, type=Path, help="Path to vacancy-agent-card.json")
    parser.add_argument("--submolt", required=True, help="Target submolt for the post.")
    parser.add_argument("--dry-run", action="store_true", help="Validate + print; do not post.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings()
    return post_vacancy(
        card_path=args.card,
        submolt=args.submolt,
        settings=settings,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(cli())
