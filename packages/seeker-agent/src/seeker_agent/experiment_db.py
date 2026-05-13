"""Persistence layer for the Seeker Agent (design §9).

Writes structured records to the isolated experiment database. All inserts
are append-only (the role has no DELETE privilege). The dedup key for
classifications is ``(venue, post_id)`` — re-classifying a post is a no-op.

The orchestrator opens one :class:`ExperimentDB` per tick (context manager),
which holds a single psycopg2 connection. Per-tick rather than per-post to
avoid connection overhead with the free-tier pacing budget.

Module is `None`-tolerant: when no experiment_db_url is configured (e.g.
S295 dry runs, or local testing without the DB), the methods are no-ops
that return sentinel ``None`` values. Callers must not rely on returned
IDs being non-None in non-persisted mode.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterator

from .classifier import Classification, PostRecord

log = logging.getLogger(__name__)

# Truncation matches design §9.1 raw_post_excerpt field.
_POST_EXCERPT_CHARS = 500


@dataclass
class WatermarkRow:
    arm: str
    channel: str
    last_id_seen: str | None
    last_tick_at: str | None


class ExperimentDB:
    """Connection-holding write+read API to the experiment database.

    Use as a context manager::

        with ExperimentDB(url) as db:
            cid = db.log_classification(c, post)
            db.log_action(verb="classify_post", outcome="ok", classification_id=cid)

    When ``url`` is ``None`` or empty, the context manager yields a no-op
    instance whose methods do nothing but return defensible values. This
    keeps the orchestrator's call sites uniform.
    """

    def __init__(self, url: str | None) -> None:
        self.url: str | None = url
        self._conn = None
        self._enabled: bool = bool(url)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "ExperimentDB":
        if not self._enabled:
            return self
        import psycopg2

        self._conn = psycopg2.connect(self.url)
        # Each method commits its own writes. Default is autocommit off,
        # which means an explicit commit per call. We keep transactions
        # short and per-write to bound rollback scope.
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            try:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
            finally:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ #
    # Writers                                                            #
    # ------------------------------------------------------------------ #

    def log_classification(
        self, classification: Classification, post: PostRecord
    ) -> int | None:
        """INSERT or NO-OP via UNIQUE (venue, post_id). Returns the row id.

        When the row already exists (re-tick on overlapping watermark),
        returns the existing id rather than crashing. When persistence
        is disabled, returns ``None``.
        """
        if not self._enabled or self._conn is None:
            return None

        excerpt = (post.post_text or "")[:_POST_EXCERPT_CHARS]
        geo = classification.extracted_geography
        params = (
            post.venue,
            post.submolt_or_channel,
            post.post_id,
            post.observed_at,
            classification.is_job_shaped,
            float(classification.relevance),
            classification.extracted_role_title,
            classification.extracted_role_family,
            classification.extracted_seniority,
            classification.extracted_company,
            geo.country_hint,
            geo.remote_hint,
            classification.has_vacancy_card_url,
            classification.vacancy_card_url,
            json.dumps(classification.spam_signals or []),
            classification.language_detected,
            classification.reasoning,
            classification.model,
            classification.prompt_version,
            classification.latency_ms,
            None,  # cost_usd_estimate — not estimated yet (S297 observability item)
            excerpt,
        )

        sql_insert = """
            INSERT INTO classifications (
                venue, submolt_or_channel, post_id, observed_at,
                is_job_shaped, relevance,
                extracted_role_title, extracted_role_family, extracted_seniority,
                extracted_company, extracted_country_hint, extracted_remote_hint,
                has_vacancy_card_url, vacancy_card_url,
                spam_signals, language_detected, reasoning,
                model, prompt_version, latency_ms, cost_usd_estimate,
                raw_post_excerpt
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s::jsonb, %s, %s,
                %s, %s, %s, %s,
                %s
            )
            ON CONFLICT (venue, post_id) DO NOTHING
            RETURNING id
        """
        sql_lookup = "SELECT id FROM classifications WHERE venue=%s AND post_id=%s"

        with self._conn.cursor() as cur:
            cur.execute(sql_insert, params)
            row = cur.fetchone()
            if row is None:
                # Dedup hit — RETURNING is empty on ON CONFLICT DO NOTHING.
                # Look up the existing id.
                cur.execute(sql_lookup, (post.venue, post.post_id))
                row = cur.fetchone()
            self._conn.commit()
            return int(row[0]) if row else None

    def log_action(
        self,
        verb: str,
        outcome: str,
        classification_id: int | None = None,
        gate_name: str | None = None,
        details: dict[str, Any] | None = None,
        completed_at_now: bool = True,
    ) -> int | None:
        """Append an action row. ``details`` is JSONB."""
        if not self._enabled or self._conn is None:
            return None
        sql = """
            INSERT INTO actions (
                classification_id, verb, outcome, gate_name, details_jsonb, completed_at
            ) VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id
        """
        completed_at = "NOW()"  # not used; psycopg2 doesn't accept bare SQL fragments in params
        with self._conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    classification_id,
                    verb,
                    outcome,
                    gate_name,
                    json.dumps(details or {}),
                    None,  # completed_at — we let DEFAULT NULL stand; tick is fast so completion == start
                ),
            )
            row = cur.fetchone()
            self._conn.commit()
            return int(row[0]) if row else None

    def log_error(
        self,
        arm: str,
        error_class: str,
        error_message: str,
        channel: str | None = None,
        verb: str | None = None,
        classification_id: int | None = None,
        raw_response: str | None = None,
    ) -> int | None:
        if not self._enabled or self._conn is None:
            return None
        sql = """
            INSERT INTO errors (
                arm, channel, verb, classification_id,
                error_class, error_message, raw_response
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        with self._conn.cursor() as cur:
            cur.execute(
                sql,
                (arm, channel, verb, classification_id, error_class, error_message, raw_response),
            )
            row = cur.fetchone()
            self._conn.commit()
            return int(row[0]) if row else None

    def log_audit_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        arm: str | None = None,
        channel: str | None = None,
        classification_id: int | None = None,
    ) -> int | None:
        """Append a row to audit_events. JSONB payload absorbs event-specific fields.

        Used by :func:`audit.emit` for lifecycle + observability events
        (rate_limit_observation, tick_complete, fetch_failed, etc).
        """
        if not self._enabled or self._conn is None:
            return None
        sql = """
            INSERT INTO audit_events (
                event_type, arm, channel, classification_id, payload
            ) VALUES (%s, %s, %s, %s, %s::jsonb)
            RETURNING id
        """
        with self._conn.cursor() as cur:
            cur.execute(
                sql,
                (event_type, arm, channel, classification_id, json.dumps(payload or {}, default=str)),
            )
            row = cur.fetchone()
            self._conn.commit()
            return int(row[0]) if row else None

    # ------------------------------------------------------------------ #
    # Watermarks                                                         #
    # ------------------------------------------------------------------ #

    def get_watermark(self, arm: str, channel: str) -> str | None:
        """Return the last_id_seen for an (arm, channel), or None if unset."""
        if not self._enabled or self._conn is None:
            return None
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT last_id_seen FROM watermarks WHERE arm=%s AND channel=%s",
                (arm, channel),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def advance_watermark(
        self, arm: str, channel: str, last_id_seen: str | None
    ) -> None:
        """Upsert the watermark for (arm, channel). Sets last_tick_at=NOW()."""
        if not self._enabled or self._conn is None:
            return
        sql = """
            INSERT INTO watermarks (arm, channel, last_id_seen, last_tick_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (arm, channel) DO UPDATE
                SET last_id_seen = EXCLUDED.last_id_seen,
                    last_tick_at = EXCLUDED.last_tick_at
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (arm, channel, last_id_seen))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Card dedup                                                         #
    # ------------------------------------------------------------------ #

    def has_seen_card(self, card_url: str) -> bool:
        if not self._enabled or self._conn is None:
            return False
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM cards_seen WHERE card_url=%s LIMIT 1", (card_url,))
            return cur.fetchone() is not None

    def record_card_seen(
        self,
        card_url: str,
        handshake_initiated: bool = False,
        handshake_outcome: str | None = None,
    ) -> None:
        if not self._enabled or self._conn is None:
            return
        sql = """
            INSERT INTO cards_seen (card_url, handshake_initiated_at, handshake_outcome)
            VALUES (%s, %s, %s)
            ON CONFLICT (card_url) DO UPDATE
                SET handshake_initiated_at = COALESCE(cards_seen.handshake_initiated_at, EXCLUDED.handshake_initiated_at),
                    handshake_outcome = COALESCE(EXCLUDED.handshake_outcome, cards_seen.handshake_outcome)
        """
        from datetime import datetime, timezone
        initiated_at = datetime.now(timezone.utc) if handshake_initiated else None
        with self._conn.cursor() as cur:
            cur.execute(sql, (card_url, initiated_at, handshake_outcome))
            self._conn.commit()
