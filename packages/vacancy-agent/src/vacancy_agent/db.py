"""S305 — Sync DB writer for vacancy-agent audit events.

Tiny module: one function that opens a short-lived psycopg2 connection,
inserts a single row into ``audit_events``, and closes. No pool, no async,
no retry queue — matches the vacancy-agent's intentionally minimal shape.

Audit failures NEVER kill the post — they log a warning and return.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def log_audit_event(
    db_url: str,
    *,
    event_type: str,
    arm: str | None = None,
    channel: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 3.0,
) -> bool:
    """Insert one row into ``audit_events``. Returns True on success.

    Never raises. On failure, logs a warning and returns False — the caller
    must not depend on this for correctness.
    """
    try:
        import psycopg2  # local import — keeps the module importable without the dep
        import psycopg2.extras
    except ImportError:
        log.warning("psycopg2 not installed — audit_events write skipped")
        return False

    payload_json = json.dumps(payload or {}, default=str)

    try:
        conn = psycopg2.connect(db_url, connect_timeout=int(timeout_seconds))
        try:
            with conn:  # transaction
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO audit_events (event_type, arm, channel, payload)
                        VALUES (%s, %s, %s, %s::jsonb)
                        """,
                        (event_type, arm, channel, payload_json),
                    )
        finally:
            conn.close()
        return True
    except Exception as exc:
        log.warning("audit_events insert failed (event_type=%s): %s", event_type, exc)
        return False
