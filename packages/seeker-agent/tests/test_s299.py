"""S299 tests: rate-limit observability + audit_events persistence.

Unit-only here — the integration of audit.emit + ExperimentDB is exercised
through the existing test_persistence.py marker pattern.
"""

from __future__ import annotations

import os

import httpx
import pytest

from seeker_agent.audit import emit, _DB_SKIP_EVENT_TYPES
from seeker_agent.experiment_db import ExperimentDB
from seeker_agent.providers.mistral import (
    MistralProvider,
    _parse_rate_limit_headers,
)

requires_db = pytest.mark.skipif(
    not os.environ.get("EXPERIMENT_DB_URL"),
    reason="EXPERIMENT_DB_URL not set",
)


# --------------------------------------------------------------------------- #
# _parse_rate_limit_headers                                                   #
# --------------------------------------------------------------------------- #


def test_parse_rate_limit_headers_extracts_mistral_keys():
    headers = {
        "Content-Type": "application/json",
        "x-ratelimit-limit-req-minute": "50",
        "x-ratelimit-limit-tokens-minute": "50000",
        "x-ratelimit-remaining-tokens-minute": "47300",
        "x-ratelimit-remaining-req-minute": "49",
        "x-ratelimit-tokens-query-cost": "1300",
    }
    out = _parse_rate_limit_headers(headers)
    assert out["limit_req_minute"] == 50
    assert out["limit_tokens_minute"] == 50000
    assert out["remaining_tokens_minute"] == 47300
    assert out["remaining_req_minute"] == 49
    assert out["tokens_query_cost"] == 1300
    assert "observed_at" in out
    # Non-ratelimit headers excluded
    assert "content_type" not in out


def test_parse_rate_limit_headers_includes_retry_after():
    headers = {"Retry-After": "5"}
    out = _parse_rate_limit_headers(headers)
    assert out["retry_after"] == 5  # underscore not applied to non-x-ratelimit


def test_parse_rate_limit_headers_handles_non_int_values():
    """Some headers (e.g. Reset times) may be non-integer; preserve as string."""
    headers = {
        "x-ratelimit-reset-tokens-minute": "2026-05-13T12:00:00Z",
        "x-ratelimit-remaining-req-minute": "49",
    }
    out = _parse_rate_limit_headers(headers)
    assert out["reset_tokens_minute"] == "2026-05-13T12:00:00Z"
    assert out["remaining_req_minute"] == 49


def test_parse_rate_limit_headers_empty_when_no_relevant_headers():
    out = _parse_rate_limit_headers({"Content-Type": "application/json"})
    # Only observed_at
    assert list(out.keys()) == ["observed_at"]


# --------------------------------------------------------------------------- #
# MistralProvider.last_rate_limit_observation                                #
# --------------------------------------------------------------------------- #


def test_provider_last_rate_limit_observation_is_none_at_construction():
    p = MistralProvider(
        api_key="test-key",
        prompt_template="dummy",
        client=httpx.Client(),
    )
    assert p.last_rate_limit_observation is None


def test_provider_captures_headers_after_200(httpx_mock):
    """A mocked 200 response with x-ratelimit headers populates the observation."""
    from seeker_agent.classifier import PostRecord

    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        method="POST",
        status_code=200,
        json={
            "model": "mistral-small-latest",
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"is_job_shaped": true, "relevance": 0.9, '
                            '"extracted_role_title": "Eng", "extracted_role_family": null, '
                            '"extracted_seniority": null, "extracted_company": "X", '
                            '"extracted_geography": {"country_hint": null, "remote_hint": null}, '
                            '"has_vacancy_card_url": false, "vacancy_card_url": null, '
                            '"spam_signals": [], "language_detected": "en", '
                            '"reasoning": "ok", "model": "mistral-small-latest", '
                            '"prompt_version": "seeker-classifier-v0.3"}'
                        )
                    }
                }
            ],
        },
        headers={
            "x-ratelimit-limit-req-minute": "50",
            "x-ratelimit-remaining-req-minute": "48",
            "x-ratelimit-limit-tokens-minute": "50000",
            "x-ratelimit-remaining-tokens-minute": "46100",
        },
    )

    p = MistralProvider(
        api_key="test-key",
        prompt_template="dummy",
        min_gap_seconds=0.0,
        client=httpx.Client(),
    )
    post = PostRecord(
        venue="gonzo_test", post_id="rl-1",
        post_text="We're hiring.", observed_at="2026-05-13T00:00:00+00:00",
        submolt_or_channel="gonzo_test",
    )
    p.classify(post)

    obs = p.last_rate_limit_observation
    assert obs is not None
    assert obs["limit_req_minute"] == 50
    assert obs["remaining_req_minute"] == 48
    assert obs["limit_tokens_minute"] == 50000
    assert obs["remaining_tokens_minute"] == 46100


# --------------------------------------------------------------------------- #
# audit.emit                                                                  #
# --------------------------------------------------------------------------- #


def test_audit_emit_skip_list_protects_post_classified():
    assert "post_classified" in _DB_SKIP_EVENT_TYPES


def test_audit_emit_no_db_just_stderr(capsys):
    emit({"event": "fetch_failed", "channel": "gonzo_x", "error": "boom"})
    captured = capsys.readouterr()
    # stderr starts with AUDIT-EVENT prefix (renamed from AUDIT-DB-PENDING)
    assert "AUDIT-EVENT " in captured.err
    assert "fetch_failed" in captured.err


def test_audit_emit_with_noop_db_does_not_crash():
    """ExperimentDB(None) is the disabled mode — log_audit_event is a no-op."""
    with ExperimentDB(None) as db:
        emit({"event": "tick_complete", "arm": "gonzo", "channel": "gonzo_test",
              "n_classified": 5}, db)
    # No assertion: just confirm it doesn't raise.


def test_audit_emit_post_classified_skipped_for_db_but_still_stderr(capsys):
    """Per-post events stay out of audit_events to avoid duplicating classifications."""
    class _StubDB:
        def __init__(self):
            self.calls = []
        def log_audit_event(self, **kwargs):
            self.calls.append(kwargs)

    stub = _StubDB()
    emit({"event": "post_classified", "venue": "gonzo_x", "post_id": "p1"}, stub)
    captured = capsys.readouterr()
    assert "AUDIT-EVENT " in captured.err
    assert "post_classified" in captured.err
    # But the DB was NOT called
    assert stub.calls == [], "post_classified should be skipped from DB"


def test_audit_emit_db_failure_is_swallowed():
    """A DB write error must not kill the tick."""
    class _ExplodingDB:
        def log_audit_event(self, **kwargs):
            raise RuntimeError("simulated DB failure")
    # Should not raise
    emit({"event": "tick_complete", "arm": "gonzo"}, _ExplodingDB())


# --------------------------------------------------------------------------- #
# ExperimentDB.log_audit_event                                                #
# --------------------------------------------------------------------------- #


def test_log_audit_event_disabled_when_url_none():
    with ExperimentDB(None) as db:
        assert db.log_audit_event(event_type="x", payload={"k": "v"}) is None


@requires_db
def test_log_audit_event_persists(experiment_db_url):
    import time
    with ExperimentDB(experiment_db_url) as db:
        eid = db.log_audit_event(
            event_type="rate_limit_observation",
            arm="gonzo",
            channel="gonzo_test",
            payload={
                "provider": "mistral",
                "limit_req_minute": 50,
                "remaining_req_minute": 48,
                "observed_at": "2026-05-13T11:00:00+00:00",
            },
        )
    assert eid is not None

    import psycopg2
    with psycopg2.connect(experiment_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_type, arm, channel, payload FROM audit_events WHERE id = %s",
                (eid,),
            )
            row = cur.fetchone()
            assert row[0] == "rate_limit_observation"
            assert row[1] == "gonzo"
            assert row[2] == "gonzo_test"
            assert row[3]["limit_req_minute"] == 50
            assert row[3]["provider"] == "mistral"


@pytest.fixture
def experiment_db_url():
    url = os.environ.get("EXPERIMENT_DB_URL")
    if not url:
        pytest.skip("EXPERIMENT_DB_URL not set")
    return url
