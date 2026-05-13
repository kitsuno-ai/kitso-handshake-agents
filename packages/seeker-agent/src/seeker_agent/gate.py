"""The deterministic gate — the heart of the §3 fence pattern.

Every proposal from the LLM passes through this module before any verb
outside the fence is executed. Each gate is a small pure function with
explicit inputs; nothing here does I/O. The orchestrator collects gate
decisions, audits drops, and proceeds only when every applicable gate
returns ``allowed=True``.

Design source: ``/opt/sf4l-staging/docs/seeker-agent-design.md`` §3.2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .classifier import Classification

GateName = Literal[
    "relevance_threshold",
    "card_url_allowlist",
    "card_schema_valid",
    "card_already_seen",
    "field_note_disabled",
    "field_note_rate_limit",
    "field_note_pii_check",
    "field_note_length",
    "kill_switch",
    "submolt_allowlist",
]


@dataclass(frozen=True)
class GateDecision:
    """Outcome of a single gate check.

    `allowed=True` and `gate=None` means "no gate applies, proceed".
    `allowed=False` carries the gate name and a human-readable reason; the
    reason is suitable for audit logging but never fed back to the LLM.
    """

    allowed: bool
    gate: GateName | None = None
    reason: str | None = None

    @classmethod
    def allow(cls) -> "GateDecision":
        return cls(allowed=True, gate=None, reason=None)

    @classmethod
    def deny(cls, gate: GateName, reason: str) -> "GateDecision":
        return cls(allowed=False, gate=gate, reason=reason)


# --------------------------------------------------------------------------- #
# Kill switch                                                                 #
# --------------------------------------------------------------------------- #


def check_kill_switch(kill_file_exists: bool) -> GateDecision:
    """If the kill file is present, no verb may execute."""
    if kill_file_exists:
        return GateDecision.deny("kill_switch", "kill file present")
    return GateDecision.allow()


# --------------------------------------------------------------------------- #
# Submolt allowlist (Moltbook arm only)                                       #
# --------------------------------------------------------------------------- #


def check_submolt(submolt: str, allowed: list[str]) -> GateDecision:
    """Fetching is restricted to submolts named in `MOLTBOOK_ALLOWED_SUBMOLTS`."""
    if not allowed:
        return GateDecision.deny("submolt_allowlist", "no submolts configured")
    if submolt not in allowed:
        return GateDecision.deny(
            "submolt_allowlist",
            f"submolt {submolt!r} not in allowlist",
        )
    return GateDecision.allow()


# --------------------------------------------------------------------------- #
# Relevance threshold — applies to ANY further action after classification    #
# --------------------------------------------------------------------------- #


def check_relevance(classification: Classification, threshold: float) -> GateDecision:
    """Drop the proposal if relevance is below the configured threshold.

    The classifier always returns a classification; the gate is what decides
    whether the orchestrator looks at it twice.
    """
    if classification.relevance < threshold:
        return GateDecision.deny(
            "relevance_threshold",
            f"relevance {classification.relevance:.2f} < {threshold:.2f}",
        )
    return GateDecision.allow()


# --------------------------------------------------------------------------- #
# Card URL allowlist                                                          #
# --------------------------------------------------------------------------- #


def check_card_url(url: str | None, allowlist_pattern: str) -> GateDecision:
    """Card URLs must match the allowlist regex exactly.

    v1 = our own cards only (`^https://kitsuno\\.ai/handshake/v0\\.1/vacancies/[a-z0-9-]+\\.json$`).
    """
    if not url:
        return GateDecision.deny("card_url_allowlist", "no card URL")
    if not re.fullmatch(allowlist_pattern, url):
        return GateDecision.deny(
            "card_url_allowlist",
            f"URL does not match allowlist: {url}",
        )
    return GateDecision.allow()


# --------------------------------------------------------------------------- #
# Card schema validity                                                        #
# --------------------------------------------------------------------------- #


def check_card_schema_valid(card: dict | None, validate_fn) -> GateDecision:
    """Use the injected validator to confirm the fetched card matches the v0.1 schema.

    `validate_fn` is a callable returning a result with `.ok: bool` and `.errors: list`.
    The vacancy-agent's :func:`cards.validate_card` fits this contract; tests inject a
    stub. Keeping the validator injected means this module has no dependency on
    a particular schema fetcher.
    """
    if card is None:
        return GateDecision.deny("card_schema_valid", "card body is None")

    result = validate_fn(card)
    if not getattr(result, "ok", False):
        errs = getattr(result, "errors", None) or ["unknown validation error"]
        return GateDecision.deny(
            "card_schema_valid",
            f"card invalid: {errs[0] if isinstance(errs, list) else errs}",
        )
    return GateDecision.allow()


# --------------------------------------------------------------------------- #
# Dedup                                                                       #
# --------------------------------------------------------------------------- #


def check_card_not_seen(url: str, cards_seen: set[str]) -> GateDecision:
    """Each card URL may initiate at most one handshake."""
    if url in cards_seen:
        return GateDecision.deny("card_already_seen", "card URL already initiated")
    return GateDecision.allow()


# --------------------------------------------------------------------------- #
# Field-note gates (all run for verb 7; disabled by default in v1)            #
# --------------------------------------------------------------------------- #


def check_field_note_feature_flag(enabled: bool) -> GateDecision:
    """v1 ships with field notes disabled per design §14.4."""
    if not enabled:
        return GateDecision.deny(
            "field_note_disabled",
            "FIELD_NOTE_ENABLED=false (v1 default)",
        )
    return GateDecision.allow()


def check_field_note_length(text: str, max_chars: int) -> GateDecision:
    """Field notes must fit the venue's post limit (default 280 chars)."""
    if len(text) > max_chars:
        return GateDecision.deny(
            "field_note_length",
            f"field note is {len(text)} chars, max {max_chars}",
        )
    return GateDecision.allow()


def check_field_note_rate_limit(
    last_post_at: datetime | None,
    now: datetime,
    min_interval_hours: int,
) -> GateDecision:
    """At most one field note per `min_interval_hours` window.

    First-ever post (``last_post_at is None``) is allowed.
    """
    if last_post_at is None:
        return GateDecision.allow()
    if last_post_at.tzinfo is None or now.tzinfo is None:
        return GateDecision.deny(
            "field_note_rate_limit",
            "naive datetime; use timezone-aware",
        )
    elapsed = now - last_post_at
    window = timedelta(hours=min_interval_hours)
    if elapsed < window:
        remaining = window - elapsed
        return GateDecision.deny(
            "field_note_rate_limit",
            f"last field note {elapsed} ago; need {remaining} more",
        )
    return GateDecision.allow()


def check_field_note_pii(text: str, pii_check_fn) -> GateDecision:
    """A second LLM call confirms the field note has no PII / instruction shapes.

    `pii_check_fn(text) -> bool` returns True iff the text is clean. The
    orchestrator injects the real check (a second classifier round-trip); tests
    inject a stub. We keep this module dependency-free.
    """
    if pii_check_fn(text):
        return GateDecision.allow()
    return GateDecision.deny(
        "field_note_pii_check",
        "second-LLM check flagged PII or instruction-shaped content",
    )


# --------------------------------------------------------------------------- #
# Helper: utc_now for tests that monkey-patch                                 #
# --------------------------------------------------------------------------- #


def utc_now() -> datetime:
    """Current time in UTC, factored out so tests can patch."""
    return datetime.now(timezone.utc)
