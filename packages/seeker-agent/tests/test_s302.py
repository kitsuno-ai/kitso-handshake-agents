"""S302 — MoltbookSeekerClient + fetch_next_moltbook_page + initiate_handshake."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from seeker_agent.moltbook_client import (
    CommentResult,
    MoltbookSeekerClient,
    map_moltbook_post,
)
from seeker_agent.verbs import (
    GONZO_CHANNELS_ALLOWED,
    HandshakeResult,
    MOLTBOOK_CHANNEL,
    fetch_next_moltbook_page,
    initiate_handshake,
)

API_BASE = "https://www.moltbook.com/api/v1/"
POSTS_URL = "https://www.moltbook.com/api/v1/posts"
POSTS_URL_RE = re.compile(r"^https://www\.moltbook\.com/api/v1/posts(\?.*)?$")
COMMENT_URL_RE = re.compile(
    r"^https://www\.moltbook\.com/api/v1/posts/[a-zA-Z0-9-]+/comments$"
)


def _sample_post(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "11111111-2222-3333-4444-555555555555",
        "title": "Senior Backend Engineer — Berlin / remote-friendly",
        "content": "We are Acme GmbH. Hiring a Go engineer for our payments team.",
        "type": "discussion",
        "author_id": "aaaa1111-bbbb-2222-cccc-333333333333",
        "author": {"name": "acme_jobs"},
        "submolt": "jobs",
        "upvotes": 5,
        "downvotes": 0,
        "score": 5,
        "comment_count": 0,
        "hot_score": 1.2,
        "is_pinned": False,
        "is_locked": False,
        "is_deleted": False,
        "verification_status": "verified",
        "is_spam": False,
        "created_at": "2026-05-13T10:00:00.000Z",
        "updated_at": "2026-05-13T10:00:00.000Z",
    }
    base.update(overrides)
    return base


def _sample_card() -> dict[str, Any]:
    return {
        "kitso.handshake.v1": {
            "principal_type": "hiring_entity",
            "vacancy": {
                "role_title": "Senior Backend Engineer",
                "role_family": "software_engineering",
                "seniority": "senior",
                "employment_type": "full_time",
                "geography": {"country": "DE", "remote_policy": "hybrid"},
                "compensation": {"disclosed_in_invitation": False},
            },
            "hiring_entity": {"name": "Acme GmbH", "size_band": "11-50"},
            "consent_policy": {"agent_may_invite_without_human_review": False},
        }
    }


# =========================================================================== #
# MoltbookSeekerClient — fetch_posts                                          #
# =========================================================================== #


class TestFetchPosts:

    def test_happy_path_returns_body(self, httpx_mock):
        httpx_mock.add_response(
            url=POSTS_URL_RE,
            json={
                "success": True,
                "posts": [_sample_post()],
                "has_more": False,
            },
        )
        c = MoltbookSeekerClient(api_key="k")
        body = c.fetch_posts(submolt="jobs", limit=10)
        assert body["success"] is True
        assert len(body["posts"]) == 1

    def test_passes_since_as_iso_param(self, httpx_mock):
        httpx_mock.add_response(url=POSTS_URL_RE, json={"success": True, "posts": []})
        c = MoltbookSeekerClient(api_key="k")
        c.fetch_posts(
            submolt="jobs",
            since=datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc),
            limit=5,
        )
        req = httpx_mock.get_request()
        # The URL must carry since=ISO8601 with trailing Z
        assert "since=2026-05-12T00%3A00%3A00.000Z" in str(req.url)
        assert "submolt=jobs" in str(req.url)
        assert "limit=5" in str(req.url)

    def test_naive_since_is_treated_as_utc(self, httpx_mock):
        httpx_mock.add_response(url=POSTS_URL_RE, json={"success": True, "posts": []})
        c = MoltbookSeekerClient(api_key="k")
        c.fetch_posts(since=datetime(2026, 5, 12, 0, 0, 0))  # naive
        req = httpx_mock.get_request()
        assert "since=2026-05-12T00%3A00%3A00.000Z" in str(req.url)

    def test_cursor_is_passed_through(self, httpx_mock):
        httpx_mock.add_response(url=POSTS_URL_RE, json={"success": True, "posts": []})
        c = MoltbookSeekerClient(api_key="k")
        c.fetch_posts(cursor="eyJpZCI6ImFiYyJ9")
        req = httpx_mock.get_request()
        assert "cursor=eyJpZCI6ImFiYyJ9" in str(req.url)

    def test_authorization_bearer_header_sent(self, httpx_mock):
        httpx_mock.add_response(url=POSTS_URL_RE, json={"success": True, "posts": []})
        c = MoltbookSeekerClient(api_key="MY-SECRET-KEY")
        c.fetch_posts()
        req = httpx_mock.get_request()
        assert req.headers["Authorization"] == "Bearer MY-SECRET-KEY"

    def test_non_200_returns_success_false(self, httpx_mock):
        httpx_mock.add_response(url=POSTS_URL_RE, status_code=500, content=b"boom")
        c = MoltbookSeekerClient(api_key="k")
        body = c.fetch_posts()
        assert body["success"] is False
        assert body["status"] == 500
        assert body["posts"] == []

    def test_transport_error_returns_success_false(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("dns"), url=POSTS_URL_RE)
        c = MoltbookSeekerClient(api_key="k")
        body = c.fetch_posts()
        assert body["success"] is False
        assert body["posts"] == []

    def test_malformed_json_returns_success_false(self, httpx_mock):
        httpx_mock.add_response(url=POSTS_URL_RE, content=b"{not json")
        c = MoltbookSeekerClient(api_key="k")
        body = c.fetch_posts()
        assert body["success"] is False

    def test_missing_posts_key_is_normalised(self, httpx_mock):
        """Some weird upstream response: success=true but no 'posts' field."""
        httpx_mock.add_response(url=POSTS_URL_RE, json={"success": True})
        c = MoltbookSeekerClient(api_key="k")
        body = c.fetch_posts()
        assert body["posts"] == []  # never None / never crashes downstream

    def test_empty_api_key_rejected(self):
        with pytest.raises(ValueError, match="api_key is required"):
            MoltbookSeekerClient(api_key="")


# =========================================================================== #
# MoltbookSeekerClient — post_comment                                         #
# =========================================================================== #


class TestPostComment:

    def test_happy_path(self, httpx_mock):
        httpx_mock.add_response(
            url=COMMENT_URL_RE,
            status_code=201,
            json={"success": True, "comment": {"id": "cmt-1"}},
        )
        c = MoltbookSeekerClient(api_key="k", comment_rate_limit_seconds=0)
        result = c.post_comment("post-1", "hello")
        assert result.ok is True
        assert result.comment_id == "cmt-1"
        assert result.status_code == 201

    def test_empty_post_id_rejected_locally(self):
        c = MoltbookSeekerClient(api_key="k")
        r = c.post_comment("", "hi")
        assert r.ok is False
        assert "post_id" in r.error

    def test_empty_content_rejected_locally(self):
        c = MoltbookSeekerClient(api_key="k")
        r = c.post_comment("p", "")
        assert r.ok is False
        r2 = c.post_comment("p", "   \n\t  ")
        assert r2.ok is False

    def test_rate_limited_second_call_to_same_post(self, httpx_mock):
        httpx_mock.add_response(
            url=COMMENT_URL_RE,
            status_code=201,
            json={"comment": {"id": "cmt-1"}},
        )
        c = MoltbookSeekerClient(api_key="k", comment_rate_limit_seconds=1800)
        c.post_comment("p", "first")
        r = c.post_comment("p", "second")
        assert r.ok is False
        assert "rate_limited" in r.error

    def test_rate_limit_is_per_post(self, httpx_mock):
        httpx_mock.add_response(
            url=COMMENT_URL_RE,
            status_code=201,
            json={"comment": {"id": "x"}},
        )
        httpx_mock.add_response(
            url=COMMENT_URL_RE,
            status_code=201,
            json={"comment": {"id": "y"}},
        )
        c = MoltbookSeekerClient(api_key="k", comment_rate_limit_seconds=1800)
        c.post_comment("p1", "hi")
        # different post → not rate limited
        r = c.post_comment("p2", "hi")
        assert r.ok is True

    def test_non_2xx_returns_failure(self, httpx_mock):
        httpx_mock.add_response(
            url=COMMENT_URL_RE, status_code=429, json={"error": "rate"}
        )
        c = MoltbookSeekerClient(api_key="k", comment_rate_limit_seconds=0)
        r = c.post_comment("p", "hi")
        assert r.ok is False
        assert r.status_code == 429

    def test_transport_error_returns_failure(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("dns"), url=COMMENT_URL_RE)
        c = MoltbookSeekerClient(api_key="k", comment_rate_limit_seconds=0)
        r = c.post_comment("p", "hi")
        assert r.ok is False
        assert "http_error" in r.error


# =========================================================================== #
# Post mapper                                                                 #
# =========================================================================== #


class TestMapMoltbookPost:

    def test_maps_canonical_fields(self):
        m = map_moltbook_post(_sample_post(), "gonzo_moltbook")
        assert m["post_id"] == "11111111-2222-3333-4444-555555555555"
        assert m["venue"] == "gonzo_moltbook"
        assert m["post_title"].startswith("Senior Backend Engineer")
        assert "Acme GmbH" in m["post_text"]
        assert m["observed_at"] == "2026-05-13T10:00:00.000Z"
        assert m["language_hint"] is None  # Moltbook doesn't expose

    def test_handles_missing_optional_fields(self):
        partial = {"id": "x", "created_at": "2026-05-13T10:00:00.000Z"}
        m = map_moltbook_post(partial, "gonzo_moltbook")
        assert m["post_id"] == "x"
        assert m["post_title"] == ""
        assert m["post_text"] == ""


# =========================================================================== #
# fetch_next_moltbook_page verb                                               #
# =========================================================================== #


class TestFetchNextMoltbookPage:

    def test_channel_is_in_gonzo_allowlist(self):
        """The fetch_gonzo / fetch_moltbook channel must be allowlisted to
        pass the gate check."""
        assert "gonzo_moltbook" in GONZO_CHANNELS_ALLOWED
        assert MOLTBOOK_CHANNEL == "gonzo_moltbook"

    def test_happy_path_returns_post_records(self, httpx_mock):
        httpx_mock.add_response(
            url=POSTS_URL_RE,
            json={
                "success": True,
                "posts": [_sample_post(id="p1"), _sample_post(id="p2")],
                "has_more": True,
                "next_cursor": "xyz",
            },
        )
        records, cursor = fetch_next_moltbook_page(
            submolt="jobs", since=None, limit=10, moltbook_api_key="k"
        )
        assert len(records) == 2
        assert records[0].post_id == "p1"
        assert records[0].venue == "gonzo_moltbook"
        assert cursor == "xyz"

    def test_no_more_returns_none_cursor(self, httpx_mock):
        httpx_mock.add_response(
            url=POSTS_URL_RE,
            json={"success": True, "posts": [_sample_post()], "has_more": False},
        )
        _, cursor = fetch_next_moltbook_page(
            submolt="jobs", since=None, limit=10, moltbook_api_key="k"
        )
        assert cursor is None

    def test_skips_posts_with_no_id(self, httpx_mock):
        httpx_mock.add_response(
            url=POSTS_URL_RE,
            json={
                "success": True,
                "posts": [
                    _sample_post(id="good"),
                    {"title": "missing id"},
                    "not even a dict",
                ],
            },
        )
        records, _ = fetch_next_moltbook_page(
            submolt="jobs", since=None, limit=10, moltbook_api_key="k"
        )
        assert len(records) == 1
        assert records[0].post_id == "good"

    def test_empty_api_key_returns_empty(self, httpx_mock):
        records, cursor = fetch_next_moltbook_page(
            submolt="jobs", since=None, limit=10, moltbook_api_key=""
        )
        assert records == []
        assert cursor is None
        # No HTTP requests should have been made
        assert httpx_mock.get_requests() == []

    def test_upstream_failure_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url=POSTS_URL_RE, status_code=503)
        records, cursor = fetch_next_moltbook_page(
            submolt="jobs", since=None, limit=10, moltbook_api_key="k"
        )
        assert records == []
        assert cursor is None


# =========================================================================== #
# initiate_handshake verb                                                     #
# =========================================================================== #


class TestInitiateHandshake:

    def test_no_moltbook_post_returns_clear_error(self):
        """Vacancy cards observed on non-Moltbook venues have no v0.1
        transport — must NOT attempt the request."""
        r = initiate_handshake(
            card=_sample_card(),
            card_url="https://kitsuno.ai/handshake/v0.1/vacancies/x.json",
            moltbook_post_id=None,
            moltbook_api_key="k",
        )
        assert r.ok is False
        assert "v01_transport_only" in r.error

    def test_missing_api_key_returns_clear_error(self):
        r = initiate_handshake(
            card=_sample_card(),
            card_url="https://kitsuno.ai/handshake/v0.1/vacancies/x.json",
            moltbook_post_id="post-1",
            moltbook_api_key="",
        )
        assert r.ok is False
        assert "moltbook_api_key_unset" in r.error

    def test_invalid_card_shape_rejected(self):
        r = initiate_handshake(
            card={"not_handshake": True},
            card_url="https://kitsuno.ai/handshake/v0.1/vacancies/x.json",
            moltbook_post_id="post-1",
            moltbook_api_key="k",
        )
        assert r.ok is False
        assert "card_shape_invalid" in r.error

    def test_happy_path_posts_comment_and_returns_id(self, httpx_mock):
        httpx_mock.add_response(
            url=COMMENT_URL_RE,
            status_code=201,
            json={"success": True, "comment": {"id": "handshake-comment-1"}},
        )
        r = initiate_handshake(
            card=_sample_card(),
            card_url="https://kitsuno.ai/handshake/v0.1/vacancies/x.json",
            moltbook_post_id="abc-123",
            moltbook_api_key="k",
        )
        assert r.ok is True
        assert r.venue_post_id == "handshake-comment-1"
        # Body of the comment should reference the card URL and the role
        req = httpx_mock.get_request()
        body_text = req.read().decode("utf-8")
        assert "https://kitsuno.ai/handshake/v0.1/vacancies/x.json" in body_text
        assert "Senior Backend Engineer" in body_text
        assert "Acme GmbH" in body_text
        assert "kitsuno_seeks" in body_text
        # And the URL targets the right post's comments endpoint
        assert str(req.url).endswith("/posts/abc-123/comments")

    def test_handles_card_with_null_optionals(self, httpx_mock):
        """If seniority/compensation/etc. are null, the reply must still post."""
        httpx_mock.add_response(
            url=COMMENT_URL_RE,
            status_code=201,
            json={"comment": {"id": "x"}},
        )
        card = {
            "kitso.handshake.v1": {
                "vacancy": {
                    "role_title": "Backend Engineer",
                    "role_family": None,
                    "seniority": None,
                    "geography": {"country": "DE", "remote_policy": None},
                },
                "hiring_entity": {"name": None},
            }
        }
        r = initiate_handshake(
            card=card,
            card_url="https://kitsuno.ai/handshake/v0.1/vacancies/y.json",
            moltbook_post_id="post-y",
            moltbook_api_key="k",
        )
        assert r.ok is True
        req = httpx_mock.get_request()
        body_text = req.read().decode("utf-8")
        # Defaults applied for null fields
        assert "(undisclosed entity)" in body_text

    def test_upstream_4xx_returns_failure_with_status(self, httpx_mock):
        httpx_mock.add_response(
            url=COMMENT_URL_RE, status_code=403, json={"error": "forbidden"}
        )
        r = initiate_handshake(
            card=_sample_card(),
            card_url="https://kitsuno.ai/handshake/v0.1/vacancies/x.json",
            moltbook_post_id="post-x",
            moltbook_api_key="k",
        )
        assert r.ok is False
        assert "403" in r.error

    def test_transport_error_returns_failure(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("dns"), url=COMMENT_URL_RE)
        r = initiate_handshake(
            card=_sample_card(),
            card_url="https://kitsuno.ai/handshake/v0.1/vacancies/x.json",
            moltbook_post_id="post-x",
            moltbook_api_key="k",
        )
        assert r.ok is False

    def test_result_is_handshake_result_instance(self):
        """Contract: every code path returns a HandshakeResult, never raises."""
        r = initiate_handshake(
            card={},
            card_url="x",
            moltbook_post_id=None,
            moltbook_api_key="k",
        )
        assert isinstance(r, HandshakeResult)
