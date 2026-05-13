"""Moltbook API client for the Seeker Agent.

Read side: pulls posts from a named submolt with watermark + cursor pagination,
maps them to :class:`PostRecord` for the classifier.

Write side: posts a comment on an existing thread — used by
:func:`seeker_agent.verbs.initiate_handshake` to surface the seeker's response
to a schema-valid vacancy card. Rate-limited and best-effort.

Endpoints discovered live (S302):

- ``GET  /api/v1/posts``                — paginated feed, supports
  ``?submolt=<slug>``, ``?since=<ISO>``, ``?cursor=<base64>``, ``?limit=N``
- ``GET  /api/v1/agents/me``            — self / identity sanity check
- ``GET  /api/v1/posts/{id}``           — single post detail
- ``GET  /api/v1/posts/{id}/comments``  — comment thread
- ``POST /api/v1/posts/{id}/comments``  — post a reply (handshake transport)
- ``GET  /api/v1/notifications``        — replies/mentions feedback channel
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class CommentResult:
    """Outcome of posting a comment (handshake transport).

    The seeker treats every send as best-effort; the dataclass lets callers
    record success/failure without trying/excepting around every call.
    """

    ok: bool
    status_code: int
    comment_id: str | None
    raw_response: dict[str, Any] | None
    error: str | None


class MoltbookSeekerClient:
    """Read-mostly Moltbook client for the seeker agent.

    Authenticates as the seeker-side agent identity (``kitsuno_seeks`` in
    production). The read methods are watermark-aware and return paginated
    results in canonical seeker shape; the write method (``post_comment``)
    is rate-limited to keep the seeker from spamming a thread if the gate
    upstream is misconfigured.
    """

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://www.moltbook.com/api/v1/",
        comment_rate_limit_seconds: int = 1800,
        user_agent: str = "kitso-handshake-seeker-agent/0.2.0",
    ):
        if not api_key:
            raise ValueError("api_key is required (set MOLTBOOK_API_KEY)")
        self._api_key = api_key
        self._api_base = api_base.rstrip("/") + "/"
        self._comment_rate_limit = comment_rate_limit_seconds
        self._user_agent = user_agent
        # Per-thread comment last-write timestamps for defensive rate limiting.
        # Key is the post_id being replied to; the gate also enforces this
        # at a higher level but a client-side belt is cheap insurance.
        self._last_comment_at: dict[str, float] = {}
        self._lock = threading.Lock()

    # --- Common ----------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }

    # --- Read ------------------------------------------------------------- #

    def fetch_posts(
        self,
        submolt: str | None = None,
        since: datetime | None = None,
        cursor: str | None = None,
        limit: int = 25,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        """GET /posts — paginated feed.

        Args:
            submolt: Optional submolt slug to filter (e.g. ``"jobs"``).
            since: Optional UTC datetime — only posts created after this point.
                Mirrors the watermark contract of :func:`fetch_next_gonzo_batch`.
            cursor: Optional opaque pagination cursor from a prior call's
                ``next_cursor`` field. Mutually compatible with ``since`` —
                Moltbook accepts both.
            limit: Page size. Moltbook accepts up to ~50 in practice; we
                default to 25 which keeps a single fetch under any reasonable
                payload cap.
            client: Optional httpx.Client for testing.
            timeout_seconds: Per-call timeout.

        Returns:
            The raw JSON body, shape::

                {
                  "success": true,
                  "posts": [ { "id", "title", "content", "author", "submolt",
                               "created_at", "comment_count", ... } ],
                  "has_more": true,
                  "next_cursor": "<base64>"
                }

            On any non-2xx, returns ``{"success": False, "status": int,
            "error": str, "posts": []}`` rather than raising — keeps the
            orchestrator simple.
        """
        params: dict[str, str] = {"limit": str(limit)}
        if submolt:
            params["submolt"] = submolt
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            # Moltbook accepts ISO 8601 UTC with the trailing Z
            params["since"] = since.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"
            )
        if cursor:
            params["cursor"] = cursor

        url = self._api_base + "posts"
        owns_client = client is None
        if owns_client:
            client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        try:
            try:
                resp = client.get(url, headers=self._headers(), params=params)
            except httpx.HTTPError as exc:
                log.warning("moltbook fetch_posts transport_error=%s", exc)
                return {"success": False, "status": 0, "error": str(exc), "posts": []}
            if resp.status_code != 200:
                return {
                    "success": False,
                    "status": resp.status_code,
                    "error": resp.text[:200],
                    "posts": [],
                }
            try:
                body = resp.json()
            except ValueError as exc:
                return {
                    "success": False,
                    "status": 200,
                    "error": f"json_parse_error: {exc}",
                    "posts": [],
                }
            # Always ensure 'posts' is a list — keeps caller iteration safe.
            if not isinstance(body.get("posts"), list):
                body["posts"] = []
            return body
        finally:
            if owns_client:
                client.close()

    # --- Write (handshake transport) -------------------------------------- #

    def post_comment(
        self,
        post_id: str,
        content: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ) -> CommentResult:
        """POST /posts/{post_id}/comments — reply to a thread.

        Used by :func:`seeker_agent.verbs.initiate_handshake` as the v0.1
        response pathway: a single public comment containing the seeker's
        handshake invitation and a link to its identity card.

        Defensively rate-limited per thread: refuses to post a second comment
        on the same post within ``comment_rate_limit_seconds``. The orchestrator
        gate (``check_card_not_seen``) already prevents this, so this client-side
        guard is belt-and-suspenders.

        Never raises on HTTP errors — returns ``CommentResult(ok=False, ...)``
        instead so the orchestrator can persist the outcome without try/except.
        """
        if not post_id:
            return CommentResult(False, 0, None, None, "empty post_id")
        if not content or not content.strip():
            return CommentResult(False, 0, None, None, "empty content")

        now = time.monotonic()
        with self._lock:
            last = self._last_comment_at.get(post_id, 0.0)
        elapsed = now - last
        if last > 0 and elapsed < self._comment_rate_limit:
            wait = self._comment_rate_limit - elapsed
            return CommentResult(
                False, 0, None, None,
                f"rate_limited: would need to wait {wait:.0f}s on post {post_id}",
            )

        url = f"{self._api_base}posts/{post_id}/comments"
        payload = {"content": content}

        owns_client = client is None
        if owns_client:
            client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        try:
            try:
                resp = client.post(url, json=payload, headers=self._headers())
            except httpx.HTTPError as exc:
                return CommentResult(False, 0, None, None, f"http_error: {exc}")

            try:
                body = resp.json()
            except ValueError:
                body = None

            if 200 <= resp.status_code < 300:
                with self._lock:
                    self._last_comment_at[post_id] = time.monotonic()
                comment_id: str | None = None
                if isinstance(body, dict):
                    comment = body.get("comment")
                    if isinstance(comment, dict):
                        comment_id = comment.get("id")
                return CommentResult(True, resp.status_code, comment_id, body, None)

            return CommentResult(
                False,
                resp.status_code,
                None,
                body if isinstance(body, dict) else None,
                f"venue_returned_{resp.status_code}",
            )
        finally:
            if owns_client:
                client.close()

    # --- Identity --------------------------------------------------------- #

    def whoami(
        self,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any] | None:
        """GET /agents/me — confirm credentials, surface karma/karma drift.

        Returns the parsed ``agent`` dict on success, ``None`` on failure.
        Used by the cron `status` subcommand for liveness checks.
        """
        owns_client = client is None
        if owns_client:
            client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        try:
            try:
                resp = client.get(
                    self._api_base + "agents/me",
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                log.warning("moltbook whoami transport_error=%s", exc)
                return None
            if resp.status_code != 200:
                return None
            try:
                body = resp.json()
            except ValueError:
                return None
            agent = body.get("agent")
            return agent if isinstance(agent, dict) else None
        finally:
            if owns_client:
                client.close()


# --------------------------------------------------------------------------- #
# Post → PostRecord mapper                                                    #
# --------------------------------------------------------------------------- #


def map_moltbook_post(raw: dict[str, Any], channel: str) -> dict[str, Any]:
    """Map a Moltbook ``post`` dict into the seeker's canonical post-row shape.

    The shape matches what ``_row_to_post_record`` consumes in verbs.py for
    market_data rows, so the rest of the pipeline doesn't have to branch on
    venue type. The verb-layer wraps the dict in a ``PostRecord``.

    Args:
        raw: A single post dict from the Moltbook /posts payload.
        channel: Channel slug the seeker uses (e.g. ``"gonzo_moltbook"``).

    Returns:
        A dict with keys: post_id, venue, post_title, post_text, observed_at,
        language_hint.
    """
    return {
        "post_id": str(raw.get("id") or ""),
        "venue": channel,
        "post_title": raw.get("title") or "",
        "post_text": raw.get("content") or "",
        "observed_at": raw.get("created_at"),
        "language_hint": None,  # Moltbook does not expose a language hint
    }
