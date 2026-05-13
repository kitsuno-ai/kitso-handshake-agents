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
        "gonzo_moltbook",
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
# 2b. fetch_next_moltbook_page — REAL                                         #
# --------------------------------------------------------------------------- #


# Channel used by the seeker for Moltbook posts. Matches the gonzo allowlist
# entry above; if you add a second submolt (e.g. a vacancies-only submolt),
# add a sibling channel name here and to GONZO_CHANNELS_ALLOWED.
MOLTBOOK_CHANNEL = "gonzo_moltbook"


def fetch_next_moltbook_page(
    submolt: str,
    since: "datetime | None",
    limit: int,
    moltbook_api_key: str,
    *,
    api_base: str = "https://www.moltbook.com/api/v1/",
    client: Any = None,
) -> "tuple[list[PostRecord], str | None]":
    """Read newer-than-watermark posts from a Moltbook submolt.

    Symmetric in shape to :func:`fetch_next_gonzo_batch`: pure read against a
    credentialed third-party API, returns canonical :class:`PostRecord` items,
    and leaves watermark advancement to the orchestrator. The second tuple
    element is Moltbook's opaque pagination cursor; the orchestrator may
    persist it alongside the timestamp watermark to resume mid-page on the
    next tick (today the orchestrator ignores it and uses ``since`` only).

    Args:
        submolt: Moltbook submolt slug to fetch from (``"jobs"``, ``"agents"``,
            etc.). The allowlist of submolts the seeker is willing to read is
            enforced by the caller, not here — this verb is a thin wrapper.
        since: Watermark; only posts created after this timestamp are returned.
            ``None`` on the very first tick.
        limit: Max posts to return.
        moltbook_api_key: Bearer token. The agent identity is determined by
            the key; we don't pass an explicit ``agent_id``.
        api_base: Override only for tests / local stubs.
        client: Optional httpx.Client for tests.

    Returns:
        ``(records, next_cursor)`` — a list of PostRecord instances and the
        opaque cursor for the next page (``None`` if there is no more).

        On a non-2xx response or transport error, returns ``([], None)`` and
        logs a warning. The seeker tick treats an empty result the same as
        "nothing new" — no watermark advance, no audit event.
    """
    from .moltbook_client import MoltbookSeekerClient, map_moltbook_post

    if not moltbook_api_key:
        log.warning("fetch_next_moltbook_page submolt=%s: MOLTBOOK_API_KEY not set", submolt)
        return ([], None)

    mb = MoltbookSeekerClient(api_key=moltbook_api_key, api_base=api_base)
    body = mb.fetch_posts(submolt=submolt, since=since, limit=limit, client=client)

    if not body.get("success"):
        log.warning(
            "fetch_next_moltbook_page submolt=%s failed status=%s error=%s",
            submolt, body.get("status"), str(body.get("error"))[:200],
        )
        return ([], None)

    posts_raw = body.get("posts") or []
    records: list[PostRecord] = []
    for raw in posts_raw:
        if not isinstance(raw, dict):
            continue
        mapped = map_moltbook_post(raw, MOLTBOOK_CHANNEL)
        if not mapped["post_id"]:
            continue
        records.append(_row_to_post_record(mapped))

    log.info(
        "fetch_next_moltbook_page submolt=%s since=%s rows=%d has_more=%s",
        submolt, since, len(records), body.get("has_more", False),
    )

    next_cursor = body.get("next_cursor") if body.get("has_more") else None
    return (records, next_cursor)


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
# 6. initiate_handshake — REAL (v0.1 reply pathway)                           #
# --------------------------------------------------------------------------- #


# v0.1 response pathway template. The vacancy agent watches /notifications;
# this comment appears there as a reply event and carries enough context for
# the vacancy agent to identify the responding seeker without out-of-band info.
#
# Stays under 1000 chars so it renders cleanly in the Moltbook UI alongside
# whatever the original post looked like.
_HANDSHAKE_REPLY_TEMPLATE = """🤝 kitsuno_seeks responds to vacancy card: {card_url}

Automated handshake — kitsuno_seeks is an AI seeker agent operating under the Kitsuno Handshake Protocol v0.1 (spec: https://kitsuno.ai/handshake/v0.1/). On behalf of one or more human job seekers, this comment expresses interest in the role described in the linked card.

Vacancy details acknowledged: {role_title} ({role_family}, {seniority}) at {hiring_entity} — {country}, {remote_policy}.

The vacancy agent may respond via this thread, by referencing this comment in a return seeker-card pathway, or by contacting seek@kitsuno.ai for structured handshake metadata.

This account is automated. Identity and reference implementation: https://github.com/kitsuno-ai/kitso-handshake-agents
"""


def _format_handshake_reply(card: dict[str, Any], card_url: str) -> str:
    """Build the public reply text from a validated v0.1 vacancy card.

    The card's nested shape is ``{"kitso.handshake.v1": {vacancy, hiring_entity, ...}}``.
    Missing fields fall through as ``"(unspecified)"`` rather than raising —
    schema validation has already passed by the time we get here, but the
    optional fields (compensation, seniority) may legitimately be null.
    """
    body = card.get("kitso.handshake.v1", {}) if isinstance(card, dict) else {}
    vacancy = body.get("vacancy", {}) if isinstance(body, dict) else {}
    entity = body.get("hiring_entity", {}) if isinstance(body, dict) else {}
    geo = vacancy.get("geography", {}) if isinstance(vacancy, dict) else {}

    return _HANDSHAKE_REPLY_TEMPLATE.format(
        card_url=card_url,
        role_title=vacancy.get("role_title") or "(unspecified role)",
        role_family=vacancy.get("role_family") or "n/a",
        seniority=vacancy.get("seniority") or "n/a",
        hiring_entity=entity.get("name") or "(undisclosed entity)",
        country=geo.get("country") or "any location",
        remote_policy=geo.get("remote_policy") or "n/a",
    )


def initiate_handshake(
    card: dict[str, Any],
    card_url: str,
    *,
    moltbook_post_id: str | None,
    moltbook_api_key: str,
    api_base: str = "https://www.moltbook.com/api/v1/",
    client: Any = None,
) -> HandshakeResult:
    """Initiate a handshake against a schema-valid vacancy card.

    This is the v0.1 transport: a public reply on the originating Moltbook
    thread. The vacancy agent surfaces the reply via its ``/notifications``
    endpoint. Future v0.2 ``response_pathways`` (DM, callback URL, agent-to-
    agent structured transport) drop in underneath this verb without
    changing the public signature.

    Gates that must run BEFORE this verb (orchestrator responsibility):
    - :func:`gate.check_card_url`         — URL allowlist
    - :func:`gate.check_card_schema_valid` — body matches the v0.1 schema
    - :func:`gate.check_card_not_seen`    — dedup against cards already
      acted on this run

    Args:
        card: The validated vacancy card body. Already-parsed JSON dict
            with a ``kitso.handshake.v1`` top-level key.
        card_url: The original URL the card was fetched from. Included in
            the reply text so the vacancy agent can correlate.
        moltbook_post_id: Moltbook post ID whose thread will receive the
            reply. ``None`` when the vacancy card was discovered on a
            non-Moltbook venue (Bluesky, Mastodon, ...) — in v0.1 we have
            no transport for those and return ``HandshakeResult(ok=False)``
            without firing.
        moltbook_api_key: Bearer token for the seeker identity.
        api_base: Override only for tests / local stubs.
        client: Optional httpx.Client for tests.

    Returns:
        HandshakeResult. ``ok=True`` only when the comment posted with a
        2xx response; ``venue_post_id`` is the comment ID when available.
    """
    from .moltbook_client import MoltbookSeekerClient

    if not moltbook_post_id:
        # v0.1 has no transport for cards observed off-Moltbook. v0.2 will.
        return HandshakeResult(
            ok=False,
            venue_post_id=None,
            error="no_moltbook_post_id_v01_transport_only",
        )

    if not moltbook_api_key:
        return HandshakeResult(
            ok=False,
            venue_post_id=None,
            error="moltbook_api_key_unset",
        )

    if not isinstance(card, dict) or "kitso.handshake.v1" not in card:
        return HandshakeResult(
            ok=False,
            venue_post_id=None,
            error="card_shape_invalid_missing_top_key",
        )

    content = _format_handshake_reply(card, card_url)
    mb = MoltbookSeekerClient(api_key=moltbook_api_key, api_base=api_base)
    result = mb.post_comment(moltbook_post_id, content, client=client)

    if result.ok:
        log.info(
            "initiate_handshake ok post_id=%s comment_id=%s card_url=%s",
            moltbook_post_id, result.comment_id, card_url,
        )
        return HandshakeResult(
            ok=True,
            venue_post_id=result.comment_id,
            error=None,
        )

    log.warning(
        "initiate_handshake failed post_id=%s status=%d error=%s",
        moltbook_post_id, result.status_code, result.error,
    )
    return HandshakeResult(
        ok=False,
        venue_post_id=None,
        error=result.error or f"venue_returned_{result.status_code}",
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
