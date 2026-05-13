"""The sealed capability set — the six (plus one optional) named verbs.

This module is the **only** outward-facing capability surface the orchestrator
exposes. The LLM never executes these; it only proposes intent in its JSON
output. The orchestrator calls these after the gate decides.

S296 status:
- ``fetch_next_gonzo_batch`` — REAL (sf4l_prod RO read)
- ``classify_post`` — REAL (delegates to provider)
- ``log_classification`` — REAL (delegates to ExperimentDB)
- everything else — stubbed with explicit NotImplementedError + S297+ pointer
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, TYPE_CHECKING

from .classifier import Classification, ClassifierProvider, PostRecord

if TYPE_CHECKING:
    from .experiment_db import ExperimentDB

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HandshakeResult:
    ok: bool
    venue_post_id: str | None
    error: str | None


# --------------------------------------------------------------------------- #
# 1. fetch_next_moltbook_page                                                 #
# --------------------------------------------------------------------------- #


def fetch_next_moltbook_page(
    submolt: str,
    after_cursor: str | None,
    api_key: str,
    api_base: str,
) -> list[PostRecord]:
    """Poll the next batch of posts from a submolt. Read-only.

    Raises NotImplementedError in S296; venue read-client lands in S297 once
    Moltbook's read API surface is documented.
    """
    raise NotImplementedError(
        "S297: Moltbook read client not yet implemented. "
        "Design: §5 verb 1; submolt allowlist enforced by gate.check_submolt."
    )


# --------------------------------------------------------------------------- #
# 2. fetch_next_gonzo_batch — REAL                                            #
# --------------------------------------------------------------------------- #


# Allowlist of channels the verb is willing to query. Mirrors the design's
# gonzo channel set; rejecting unknown channels here protects against an LLM
# proposing an arbitrary string that happens to look like a source name.
GONZO_CHANNELS_ALLOWED = frozenset(
    {
        "gonzo_hn_whoshiring",
        "gonzo_bluesky",
        "gonzo_telegram",
        "gonzo_reddit",
        "gonzo_lobsters_whoshiring",
        "gonzo_mastodon",
    }
)


_GONZO_SQL = """
SELECT
    id::text          AS post_id,
    source            AS venue,
    title             AS post_title,
    description       AS post_text,
    description_language AS language_hint,
    gonzo_first_seen  AS observed_at
FROM market_data
WHERE source = %(channel)s
  AND ( %(since)s::timestamptz IS NULL OR gonzo_first_seen > %(since)s::timestamptz )
ORDER BY gonzo_first_seen ASC
LIMIT %(limit)s
""".strip()


def _row_to_post_record(row: dict[str, Any]) -> PostRecord:
    """Pure mapper. Easy to unit-test without a DB."""
    observed = row["observed_at"]
    if isinstance(observed, datetime):
        observed_iso = observed.astimezone(timezone.utc).isoformat()
    else:
        observed_iso = str(observed) if observed is not None else ""
    return PostRecord(
        venue=row["venue"],
        post_id=row["post_id"],
        post_text=row["post_text"] or "",
        post_title=row.get("post_title"),
        observed_at=observed_iso,
        submolt_or_channel=row["venue"],  # for gonzo, channel == venue
        language_hint=row.get("language_hint") or None,
    )


def _fetch_gonzo_rows(
    conn,
    channel: str,
    since: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Run the parameterized SELECT and return list-of-dicts.

    ``conn`` is anything with a ``cursor()`` that returns a context-manager
    cursor supporting ``execute()`` + ``description`` + ``fetchall()``. In
    production this is a psycopg2 connection; tests inject a fake.
    """
    from psycopg2.extras import RealDictCursor  # local import — keeps test mocking easy

    if channel not in GONZO_CHANNELS_ALLOWED:
        raise ValueError(
            f"channel {channel!r} not in GONZO_CHANNELS_ALLOWED; refusing to query"
        )

    params = {"channel": channel, "since": since, "limit": int(limit)}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(_GONZO_SQL, params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetch_next_gonzo_batch(
    channel: str,
    since: datetime | None,
    limit: int,
    sf4l_prod_readonly_url: str,
) -> list[PostRecord]:
    """Read newer-than-watermark rows from market_data WHERE source = :channel.

    Read-only credential; no writes. The query is parameterized; ``channel``
    is also allowlisted defensively before reaching SQL.

    The caller is responsible for advancing the watermark on success — that
    keeps watermarks transactional with the rest of the tick.
    """
    import psycopg2  # local import so the package imports cheaply

    if channel not in GONZO_CHANNELS_ALLOWED:
        raise ValueError(
            f"channel {channel!r} not in GONZO_CHANNELS_ALLOWED; refusing to query"
        )

    conn = psycopg2.connect(sf4l_prod_readonly_url)
    try:
        rows = _fetch_gonzo_rows(conn, channel, since, limit)
    finally:
        conn.close()

    log.info(
        "fetch_next_gonzo_batch channel=%s since=%s rows=%d", channel, since, len(rows)
    )
    return [_row_to_post_record(r) for r in rows]


# --------------------------------------------------------------------------- #
# 3. classify_post — REAL                                                     #
# --------------------------------------------------------------------------- #


def classify_post(post: PostRecord, provider: ClassifierProvider) -> Classification:
    """Run the classifier against a single post.

    Latency is measured here and stamped onto the Classification — the
    provider itself does not need to know about timing.
    """
    start = time.monotonic()
    result = provider.classify(post)
    elapsed_ms = int((time.monotonic() - start) * 1000)
    result.latency_ms = elapsed_ms
    return result


# --------------------------------------------------------------------------- #
# 4. read_vacancy_card                                                        #
# --------------------------------------------------------------------------- #


# Defensive cap: vacancy cards in v0.1 are small JSON documents. The valid
# fixtures in this repo are <2KB; even a generous one carrying provenance
# metadata and a verbose nice_to_haves list would not exceed ~16KB. A 256KB
# ceiling is two orders of magnitude above realistic, far below anything
# that could DoS the agent process, and is enforced before JSON parsing.
_CARD_BYTES_CAP = 256 * 1024


def read_vacancy_card(
    url: str,
    timeout_seconds: float = 10.0,
    *,
    client: "httpx.Client | None" = None,
) -> dict[str, Any] | None:
    """Fetch a vacancy card by URL, parse JSON, return the dict.

    The URL allowlist gate (:func:`gate.check_card_url`) runs BEFORE this verb
    — the orchestrator is responsible for that. Schema validation runs AFTER,
    via :func:`gate.check_card_schema_valid`. This verb does neither; it only
    fetches and parses, returning the body as a dict or ``None`` on any failure
    mode (network error, non-2xx, oversized response, malformed JSON, JSON
    that is not an object).

    Args:
        url: Card URL. Must already have passed the URL allowlist check.
        timeout_seconds: Total request timeout (connect + read).
        client: Optional pre-built ``httpx.Client`` — tests inject one with
            ``pytest-httpx`` so the fetch is mockable.

    Returns:
        Parsed JSON dict, or ``None`` if anything went wrong. Failures are
        logged at WARNING level and emitted via stderr; the orchestrator
        wraps :func:`audit.emit` around the verb to record skip events.
    """
    import httpx  # local import — keeps the package importable without httpx during stub usage

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=timeout_seconds,
            # No automatic redirects — card URLs are exact matches; a 30x
            # would indicate either a misconfiguration or an attempt to
            # exfiltrate the request to an off-allowlist host.
            follow_redirects=False,
        )
    try:
        try:
            response = client.get(url)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            log.warning("read_vacancy_card url=%s transport_error=%s", url, exc)
            return None

        if response.status_code != 200:
            log.warning(
                "read_vacancy_card url=%s status=%d (expected 200)",
                url,
                response.status_code,
            )
            return None

        content = response.content
        if len(content) > _CARD_BYTES_CAP:
            log.warning(
                "read_vacancy_card url=%s oversize bytes=%d cap=%d",
                url,
                len(content),
                _CARD_BYTES_CAP,
            )
            return None

        try:
            parsed = response.json()
        except ValueError as exc:
            log.warning("read_vacancy_card url=%s json_parse_error=%s", url, exc)
            return None

        if not isinstance(parsed, dict):
            log.warning(
                "read_vacancy_card url=%s payload not a JSON object (got %s)",
                url,
                type(parsed).__name__,
            )
            return None

        log.info("read_vacancy_card url=%s bytes=%d ok", url, len(content))
        return parsed
    finally:
        if owns_client:
            client.close()


# --------------------------------------------------------------------------- #
# 5. log_classification — REAL                                                #
# --------------------------------------------------------------------------- #


def log_classification(
    classification: Classification,
    post: PostRecord,
    experiment_db: "ExperimentDB",
) -> int | None:
    """Append-only INSERT into experiment_db.classifications. Returns row id.

    Idempotent via ``UNIQUE (venue, post_id)``. When persistence is disabled
    (``experiment_db_url`` unset), returns ``None`` and is a no-op.
    """
    return experiment_db.log_classification(classification, post)


# --------------------------------------------------------------------------- #
# 6. initiate_handshake                                                       #
# --------------------------------------------------------------------------- #


def initiate_handshake(card: dict[str, Any]) -> HandshakeResult:
    """Initiate a handshake against a schema-valid card. Stubbed in S296.

    Transport TBD per Kitso Handshake spec v0.2 response_pathways field.
    """
    raise NotImplementedError(
        "S297: handshake transport TBD (spec v0.2 response_pathways). "
        "Design: §5 verb 6; gates: URL allowlist + schema valid + dedup all run first."
    )


# --------------------------------------------------------------------------- #
# 7. post_field_note (optional, disabled in v1)                              #
# --------------------------------------------------------------------------- #


def post_field_note(text: str, target_submolt: str, api_key: str, api_base: str) -> HandshakeResult:
    """Post a field note (verb 7). Disabled in v1 per design §14.4."""
    raise NotImplementedError(
        "S297+: field note verb. v1 default is FIELD_NOTE_ENABLED=false; "
        "gates: feature flag + length + rate limit + second-LLM PII check."
    )
