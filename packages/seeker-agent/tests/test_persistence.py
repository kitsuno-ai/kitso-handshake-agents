"""S296 persistence tests.

Two layers:

1. **Unit tests** — exercise the no-op mode (``url=None``) where
   :class:`ExperimentDB` returns ``None`` / is a no-op. These run anywhere.

2. **Integration tests** — round-trip data against the real
   ``experiment-db-postgres`` container. They require the
   ``EXPERIMENT_DB_URL`` env var to be set; otherwise pytest skips them
   with a reason. Each integration test cleans up its own rows so the
   suite is rerunnable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from seeker_agent.classifier import Classification, Geography, PostRecord
from seeker_agent.experiment_db import ExperimentDB

requires_db = pytest.mark.skipif(
    not os.environ.get("EXPERIMENT_DB_URL"),
    reason="EXPERIMENT_DB_URL not set; skipping integration tests",
)


def _classification(**overrides) -> Classification:
    base = dict(
        is_job_shaped=True,
        relevance=0.85,
        extracted_role_title="Senior Engineer",
        extracted_role_family="software_engineering",
        extracted_seniority="senior",
        extracted_company="Acme",
        extracted_geography=Geography(country_hint="DE", remote_hint="fully_remote"),
        has_vacancy_card_url=False,
        vacancy_card_url=None,
        spam_signals=[],
        language_detected="en",
        reasoning="Concrete role and location named.",
        model="test/0.1",
        prompt_version="seeker-classifier-v0.2",
        latency_ms=1234,
    )
    base.update(overrides)
    return Classification(**base)


def _post(**overrides) -> PostRecord:
    base = dict(
        venue="gonzo_test",
        post_id="post-test-1",
        post_text="We're hiring a senior engineer in Berlin.",
        post_title="Senior Engineer",
        observed_at="2026-05-13T10:00:00+00:00",
        submolt_or_channel="gonzo_test",
        language_hint=None,
    )
    base.update(overrides)
    return PostRecord(**base)


# --------------------------------------------------------------------------- #
# Unit tests — no-op mode                                                     #
# --------------------------------------------------------------------------- #


def test_experiment_db_disabled_when_url_none():
    db = ExperimentDB(None)
    assert db._enabled is False


def test_experiment_db_noop_context_manager_does_not_connect():
    """ExperimentDB(None) entered + exited without psycopg2 import path firing."""
    with ExperimentDB(None) as db:
        assert db._conn is None
        # All write methods are no-ops returning None
        assert db.log_classification(_classification(), _post()) is None
        assert db.log_action(verb="classify_post", outcome="ok") is None
        assert db.log_error(arm="gonzo", error_class="x", error_message="y") is None
        assert db.get_watermark("gonzo", "gonzo_test") is None
        # advance_watermark / record_card_seen return nothing — make sure they don't raise
        db.advance_watermark("gonzo", "gonzo_test", "2026-05-13T10:00:00+00:00")
        db.record_card_seen("https://x", handshake_initiated=False)


def test_experiment_db_disabled_for_empty_string():
    """Empty string should be treated as disabled (defensive)."""
    db = ExperimentDB("")
    assert db._enabled is False


def test_experiment_db_has_seen_card_returns_false_when_disabled():
    with ExperimentDB(None) as db:
        assert db.has_seen_card("https://kitsuno.ai/handshake/v0.1/vacancies/x.json") is False


# --------------------------------------------------------------------------- #
# Integration tests — real DB                                                 #
# --------------------------------------------------------------------------- #


@requires_db
def test_round_trip_classification(experiment_db_url):
    """INSERT a classification, SELECT it back, then clean up via FK cascade."""
    pid = f"itest-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    post = _post(post_id=pid)
    c = _classification()

    with ExperimentDB(experiment_db_url) as db:
        cid = db.log_classification(c, post)
        assert cid is not None
        assert isinstance(cid, int)
        assert cid > 0

    # Verify the row is there with the right shape
    import psycopg2
    with psycopg2.connect(experiment_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT venue, post_id, is_job_shaped, relevance, model, prompt_version, "
                "extracted_role_title, latency_ms, raw_post_excerpt "
                "FROM classifications WHERE id = %s",
                (cid,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "gonzo_test"
            assert row[1] == pid
            assert row[2] is True
            assert abs(row[3] - 0.85) < 1e-5
            assert row[4] == "test/0.1"
            assert row[5] == "seeker-classifier-v0.2"
            assert row[6] == "Senior Engineer"
            assert row[7] == 1234
            assert "hiring" in row[8].lower()


@requires_db
def test_classification_idempotent_on_duplicate(experiment_db_url):
    """Second call with same (venue, post_id) returns same id, no error."""
    pid = f"dup-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    post = _post(post_id=pid)
    c = _classification()

    with ExperimentDB(experiment_db_url) as db:
        cid1 = db.log_classification(c, post)
        cid2 = db.log_classification(c, post)

    assert cid1 == cid2
    assert cid1 is not None


@requires_db
def test_log_action_with_fk_to_classification(experiment_db_url):
    pid = f"action-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    post = _post(post_id=pid)
    c = _classification()

    with ExperimentDB(experiment_db_url) as db:
        cid = db.log_classification(c, post)
        aid1 = db.log_action(verb="classify_post", outcome="measured_only", classification_id=cid)
        aid2 = db.log_action(
            verb="initiate_handshake",
            outcome="dropped_at_gate",
            classification_id=cid,
            gate_name="card_url_allowlist",
            details={"observed_url": "https://evil.example/x.json"},
        )

    assert aid1 is not None
    assert aid2 is not None

    import psycopg2
    with psycopg2.connect(experiment_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT verb, outcome, gate_name, details_jsonb FROM actions WHERE id IN (%s, %s) ORDER BY id",
                (aid1, aid2),
            )
            rows = cur.fetchall()
            assert rows[0][:3] == ("classify_post", "measured_only", None)
            assert rows[1][0] == "initiate_handshake"
            assert rows[1][1] == "dropped_at_gate"
            assert rows[1][2] == "card_url_allowlist"
            assert rows[1][3]["observed_url"] == "https://evil.example/x.json"


@requires_db
def test_log_error_stores_classification_context(experiment_db_url):
    with ExperimentDB(experiment_db_url) as db:
        eid = db.log_error(
            arm="gonzo",
            error_class="schema_invalid",
            error_message="missing 'is_job_shaped'",
            channel="gonzo_hn_whoshiring",
            verb="classify_post",
            raw_response='{"job_shaped": true}',
        )

    assert eid is not None
    import psycopg2
    with psycopg2.connect(experiment_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT arm, channel, verb, error_class, error_message, raw_response "
                "FROM errors WHERE id = %s",
                (eid,),
            )
            row = cur.fetchone()
            assert row == (
                "gonzo",
                "gonzo_hn_whoshiring",
                "classify_post",
                "schema_invalid",
                "missing 'is_job_shaped'",
                '{"job_shaped": true}',
            )


@requires_db
def test_watermark_advance_and_read(experiment_db_url):
    import time as _t
    arm, channel = "gonzo", f"itest-channel-watermark-{_t.time_ns()}"
    ts1 = "2026-05-13T10:00:00+00:00"
    ts2 = "2026-05-13T11:00:00+00:00"

    with ExperimentDB(experiment_db_url) as db:
        # No watermark yet
        assert db.get_watermark(arm, channel) is None
        # Set + read
        db.advance_watermark(arm, channel, ts1)
        assert db.get_watermark(arm, channel) == ts1
        # Advance
        db.advance_watermark(arm, channel, ts2)
        assert db.get_watermark(arm, channel) == ts2

    # Note: no DELETE — seeker_writer has no DELETE privilege (by design).
    # The test uses an `itest-` prefixed channel name that no real channel uses,
    # so leftover rows don't interfere with production data.


@requires_db
def test_card_seen_dedup(experiment_db_url):
    url = f"https://kitsuno.ai/handshake/v0.1/vacancies/itest-{os.getpid()}-card.json"

    with ExperimentDB(experiment_db_url) as db:
        assert db.has_seen_card(url) is False
        db.record_card_seen(url, handshake_initiated=False)
        assert db.has_seen_card(url) is True
        # Idempotent re-record doesn't crash
        db.record_card_seen(url, handshake_initiated=False)
        assert db.has_seen_card(url) is True


@pytest.fixture
def experiment_db_url():
    url = os.environ.get("EXPERIMENT_DB_URL")
    if not url:
        pytest.skip("EXPERIMENT_DB_URL not set")
    return url
