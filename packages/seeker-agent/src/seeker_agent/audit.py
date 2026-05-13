"""Audit logging for the Seeker Agent.

Two layers:

1. **stderr**: every event becomes a single ``AUDIT-EVENT {...}`` line. Always
   on. Useful for cron logs, local debugging, grep.

2. **experiment_db (audit_events table)**: when ``db`` is provided (an open
   :class:`ExperimentDB`), structured rows are appended for SQL-side
   observability. Per-post events (``event_type == "post_classified"``) are
   skipped because they duplicate the ``classifications`` + ``actions`` tables.

Audit shape is a flat dict, JSON-serializable. :func:`emit` adds ``timestamp``
if not present.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .experiment_db import ExperimentDB

log = logging.getLogger(__name__)


# Events that should NOT be persisted to audit_events because they're already
# captured in dedicated tables (classifications + actions). They still hit
# stderr for ops visibility.
_DB_SKIP_EVENT_TYPES: frozenset[str] = frozenset({
    "post_classified",
})

# Fields that are "structural" (lifted to dedicated columns on audit_events)
# vs "payload" (everything else goes into the JSONB column).
_STRUCTURAL_FIELDS: frozenset[str] = frozenset({
    "event", "arm", "channel", "classification_id", "timestamp",
})


def emit(event: dict[str, Any], db: "ExperimentDB | None" = None) -> None:
    """Record an audit event.

    Always writes a single ``AUDIT-EVENT {...}`` line to stderr (grep-able).
    When ``db`` is provided AND the event_type is not in the skip list,
    also appends a row to ``audit_events`` in the experiment DB.

    The ``event`` dict must have an ``"event"`` key (the event_type). It can
    optionally have ``"arm"``, ``"channel"``, ``"classification_id"`` which
    are lifted to dedicated columns; everything else lands in the JSONB
    payload column.
    """
    event = dict(event)  # copy so we can mutate
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

    # stderr — always
    print("AUDIT-EVENT " + json.dumps(event, default=str), file=sys.stderr, flush=True)

    # DB — when we have a live connection
    if db is None:
        return

    event_type = event.get("event")
    if not event_type or event_type in _DB_SKIP_EVENT_TYPES:
        return

    # Split structural fields from payload
    payload = {k: v for k, v in event.items() if k not in _STRUCTURAL_FIELDS}

    try:
        db.log_audit_event(
            event_type=event_type,
            arm=event.get("arm"),
            channel=event.get("channel"),
            classification_id=event.get("classification_id"),
            payload=payload,
        )
    except Exception as exc:
        # Audit failures must not kill the tick. Log + continue.
        log.warning("audit_events insert failed (event=%s): %s", event_type, exc)
