"""Moltbook API client.

Minimal client for posting a single submission. Enforces the venue's published
rate limit (1 post / 30 min / agent). Does not read posts, does not list submolts,
does not handle comments or upvotes — out of scope for the Vacancy Agent.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class PostResult:
    ok: bool
    status_code: int
    post_id: str | None
    raw_response: dict[str, Any] | None
    error: str | None


class MoltbookClient:
    """Single-purpose Moltbook client for posting vacancy announcements.

    Rate limit enforcement is done by the gate, not in this client — but
    the client refuses to fire more than once within the configured interval
    as a defensive belt-and-suspenders measure.
    """

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.moltbook.com/v1/",
        rate_limit_seconds: int = 1800,
        user_agent: str = "kitso-handshake-vacancy-agent/0.1.0 (+https://github.com/kitsuno-ai/kitso-handshake-agents)",
    ):
        if not api_key:
            raise ValueError("api_key is required (set MOLTBOOK_API_KEY)")
        self._api_key = api_key
        self._api_base = api_base.rstrip("/") + "/"
        self._rate_limit_seconds = rate_limit_seconds
        self._user_agent = user_agent
        self._last_post_at: float = 0.0

    def post(
        self,
        submolt: str,
        title: str,
        content: str,
        timeout_seconds: float = 30.0,
    ) -> PostResult:
        """Post a single submission to the named submolt.

        Returns a PostResult — never raises on HTTP errors. Network-level
        exceptions (DNS, connection refused) also surface as PostResult(ok=False).

        Refuses to post if called within rate_limit_seconds of the previous successful
        post — this is a defensive belt; the gate should already have stopped us.
        """
        now = time.monotonic()
        elapsed = now - self._last_post_at
        if elapsed < self._rate_limit_seconds and self._last_post_at > 0:
            wait = self._rate_limit_seconds - elapsed
            return PostResult(
                ok=False,
                status_code=0,
                post_id=None,
                raw_response=None,
                error=f"rate_limited: would need to wait {wait:.0f}s",
            )

        url = self._api_base + "posts"
        payload = {"submolt": submolt, "title": title, "content": content}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
        }

        log.info("posting to moltbook submolt=%s title_len=%d", submolt, len(title))
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return PostResult(
                ok=False,
                status_code=0,
                post_id=None,
                raw_response=None,
                error=f"http_error: {exc}",
            )

        try:
            body = resp.json()
        except ValueError:
            body = None

        if 200 <= resp.status_code < 300:
            self._last_post_at = time.monotonic()
            post_id = (body or {}).get("post", {}).get("id") if isinstance(body, dict) else None
            return PostResult(
                ok=True,
                status_code=resp.status_code,
                post_id=post_id,
                raw_response=body,
                error=None,
            )

        return PostResult(
            ok=False,
            status_code=resp.status_code,
            post_id=None,
            raw_response=body,
            error=f"venue_returned_{resp.status_code}",
        )
