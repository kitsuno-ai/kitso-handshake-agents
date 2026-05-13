"""The sealed capability set — the six (plus one optional) named verbs.

This module is the **only** outward-facing capability surface the orchestrator
exposes. The LLM never executes these; it only proposes intent in its JSON
output. The orchestrator calls these after the gate decides.

For S295, real implementations of verbs that need venue clients or the
experiment DB (`fetch_next_*`, `read_vacancy_card`, `log_classification`,
`initiate_handshake`, `post_field_note`) are stubbed with
:class:`NotImplementedError`. `classify_post` is real — it delegates to the
injected :class:`~seeker_agent.classifier.ClassifierProvider`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .classifier import Classification, ClassifierProvider, PostRecord

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

    Raises NotImplementedError in S295; venue read-client lands in S296 once
    Moltbook's read API surface is documented.
    """
    raise NotImplementedError(
        "S296: Moltbook read client not yet implemented. "
        "Design: §5 verb 1; submolt allowlist enforced by gate.check_submolt."
    )


# --------------------------------------------------------------------------- #
# 2. fetch_next_gonzo_batch                                                   #
# --------------------------------------------------------------------------- #


def fetch_next_gonzo_batch(
    channel: str,
    since_id: int | None,
    sf4l_prod_readonly_url: str,
) -> list[PostRecord]:
    """Read newer-than-watermark rows from sf4l_prod.market_data WHERE source=$channel.

    Read-only credential; no writes. S296 implementation uses psycopg2.
    """
    raise NotImplementedError(
        "S296: gonzo arm reader not yet implemented. "
        "Design: §5 verb 2; query market_data WHERE source LIKE 'gonzo_%' AND id > $since_id."
    )


# --------------------------------------------------------------------------- #
# 3. classify_post — REAL                                                     #
# --------------------------------------------------------------------------- #


def classify_post(post: PostRecord, provider: ClassifierProvider) -> Classification:
    """Run the classifier against a single post.

    Latency is measured here and stamped onto the Classification — the
    provider itself does not need to know about timing. Cost-per-call is
    estimated upstream once we have token counts (S296).
    """
    start = time.monotonic()
    result = provider.classify(post)
    elapsed_ms = int((time.monotonic() - start) * 1000)
    # Classification is a regular dataclass (not frozen) so we can stamp latency.
    result.latency_ms = elapsed_ms
    return result


# --------------------------------------------------------------------------- #
# 4. read_vacancy_card                                                        #
# --------------------------------------------------------------------------- #


def read_vacancy_card(url: str, timeout_seconds: float = 10.0) -> dict[str, Any] | None:
    """Fetch a vacancy card by URL, parse JSON, return the dict.

    The URL allowlist gate runs BEFORE this verb (orchestrator responsibility).
    Schema validation runs AFTER, via :func:`gate.check_card_schema_valid`.

    Stubbed in S295; HTTP fetch lands in S296.
    """
    raise NotImplementedError(
        "S296: card fetcher not yet implemented. "
        "Design: §5 verb 4; httpx GET with timeout, return parsed JSON or None on failure."
    )


# --------------------------------------------------------------------------- #
# 5. log_classification                                                       #
# --------------------------------------------------------------------------- #


def log_classification(
    classification: Classification,
    post: PostRecord,
    experiment_db_url: str,
) -> int:
    """Append-only INSERT into experiment_db.classifications. Returns row PK.

    Stubbed in S295; persistence lands in S296.
    """
    raise NotImplementedError(
        "S296: experiment_db writer not yet implemented. "
        "Design: §9.1; INSERT ... ON CONFLICT (venue, post_id) DO NOTHING."
    )


# --------------------------------------------------------------------------- #
# 6. initiate_handshake                                                       #
# --------------------------------------------------------------------------- #


def initiate_handshake(card: dict[str, Any]) -> HandshakeResult:
    """Initiate a handshake against a schema-valid card. Stubbed in S295.

    The transport for this is still TBD — see Kitso Handshake spec for the
    response_pathways field. Likely an email + structured response form. S296.
    """
    raise NotImplementedError(
        "S296: handshake transport TBD (spec v0.2 response_pathways). "
        "Design: §5 verb 6; gates: URL allowlist + schema valid + dedup all run first."
    )


# --------------------------------------------------------------------------- #
# 7. post_field_note (optional, disabled in v1)                              #
# --------------------------------------------------------------------------- #


def post_field_note(text: str, target_submolt: str, api_key: str, api_base: str) -> HandshakeResult:
    """Post a field note (verb 7). Disabled in v1 per design §14.4.

    The gate already rejects the call when ``FIELD_NOTE_ENABLED=false``; this
    function exists to make the capability surface visible in code review even
    though it is unreachable in v1.
    """
    raise NotImplementedError(
        "S296+: field note verb. v1 default is FIELD_NOTE_ENABLED=false; "
        "gates: feature flag + length + rate limit + second-LLM PII check."
    )
