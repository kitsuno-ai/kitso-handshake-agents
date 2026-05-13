"""Audit logging for the Seeker Agent.

S295 ships a stdout fallback only. The real :func:`emit` writes a row to the
isolated experiment DB in S296 — see design doc §9.1 for the table schema.

Audit shape is deliberately simple: a flat dict, JSON-serializable, with a
``timestamp`` field added by :func:`emit`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def emit(event: dict[str, Any], experiment_db_url: str | None = None) -> None:
    """Record an audit event.

    For S295, when `experiment_db_url` is unset (the default), the event goes
    to stderr as a single ``AUDIT {...}`` line so it is grep-able. When the
    DB URL is set, this is a no-op with a warning — the actual DB write lands
    in S296.
    """
    event = dict(event)  # copy so we can mutate
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

    if experiment_db_url:
        # TODO(s296): wire to experiment_db (isolated Postgres)
        log.info("AUDIT-DB-PENDING %s", json.dumps(event, default=str))
        return

    print("AUDIT " + json.dumps(event, default=str), file=sys.stderr, flush=True)
