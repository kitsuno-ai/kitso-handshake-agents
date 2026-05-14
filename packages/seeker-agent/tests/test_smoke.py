"""Smoke tests for the Seeker Agent.

Coverage areas:
- Config parsing + live-mode requirements per arm
- Classification schema validation (positive + negative)
- EchoProvider determinism
- Each gate as a pure function
- Orchestrator end-to-end with EchoProvider against synthetic posts
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from seeker_agent import audit, gate
from seeker_agent.classifier import (
    CLASSIFICATION_SCHEMA,
    Classification,
    ClassifierSchemaError,
    EchoProvider,
    Geography,
    PostRecord,
    validate_payload,
)
from seeker_agent.config import Settings
from seeker_agent.main import run_tick


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _clean_seeker_env(monkeypatch):
    """Strip every Settings field name from env so defaults apply."""
    for var in (
        "SEEKER_LLM_PROVIDER",
        "MISTRAL_API_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "SEEKER_RELEVANCE_THRESHOLD",
        "MOLTBOOK_ARM_ENABLED",
        "MOLTBOOK_API_KEY",
        "MOLTBOOK_ALLOWED_SUBMOLTS",
        "GONZO_ARM_ENABLED",
        "SF4L_PROD_READONLY_URL",
        "GONZO_CHANNELS",
        "FIELD_NOTE_ENABLED",
        "EXPERIMENT_DB_URL",
        "SEEKER_KILL_TOKEN",
        "SEEKER_KILL_FILE",
        "TICK_LOCK_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


def _post(
    text: str,
    *,
    venue: str = "moltbook",
    post_id: str = "p1",
    submolt: str = "hiring",
) -> PostRecord:
    return PostRecord(
        venue=venue,
        post_id=post_id,
        post_text=text,
        observed_at="2026-05-13T09:00:00+00:00",
        submolt_or_channel=submolt,
    )


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #


def test_settings_defaults(monkeypatch):
    _clean_seeker_env(monkeypatch)
    s = Settings()
    assert s.seeker_llm_provider == "mistral"
    assert s.seeker_relevance_threshold == 0.7
    assert s.field_note_enabled is False
    assert s.submolt_list() == []
    assert "gonzo_hn_whoshiring" in s.gonzo_channel_list()


def test_settings_submolt_list_parses_csv(monkeypatch):
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("MOLTBOOK_ALLOWED_SUBMOLTS", "hiring, ai-engineers ,  ,robotics")
    s = Settings()
    assert s.submolt_list() == ["hiring", "ai-engineers", "robotics"]


def test_settings_llm_credentials_mistral(monkeypatch):
    _clean_seeker_env(monkeypatch)
    s = Settings()
    ok, missing = s.llm_credentials_ok()
    assert not ok and missing == ["MISTRAL_API_KEY"]

    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    ok, missing = Settings().llm_credentials_ok()
    assert ok and missing == []


def test_settings_llm_credentials_cloudflare(monkeypatch):
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("SEEKER_LLM_PROVIDER", "cloudflare")
    s = Settings()
    ok, missing = s.llm_credentials_ok()
    assert not ok
    assert set(missing) == {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"}

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    ok, missing = Settings().llm_credentials_ok()
    assert ok and missing == []


def test_check_live_mode_moltbook_missing(monkeypatch):
    _clean_seeker_env(monkeypatch)
    s = Settings()
    missing = s.check_live_mode("moltbook")
    assert "MISTRAL_API_KEY" in missing
    assert "SEEKER_KILL_TOKEN" in missing
    assert "MOLTBOOK_API_KEY" in missing
    assert "MOLTBOOK_ALLOWED_SUBMOLTS" in missing
    assert "EXPERIMENT_DB_URL" in missing


def test_check_live_mode_gonzo_missing(monkeypatch):
    _clean_seeker_env(monkeypatch)
    s = Settings()
    missing = s.check_live_mode("gonzo")
    assert "SF4L_PROD_READONLY_URL" in missing
    assert "MOLTBOOK_API_KEY" not in missing
    assert "MOLTBOOK_ALLOWED_SUBMOLTS" not in missing


def test_check_live_mode_all_set(monkeypatch):
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setenv("SEEKER_KILL_TOKEN", "t")
    monkeypatch.setenv("MOLTBOOK_API_KEY", "mk")
    monkeypatch.setenv("MOLTBOOK_ALLOWED_SUBMOLTS", "hiring")
    monkeypatch.setenv("EXPERIMENT_DB_URL", "postgresql://x")
    monkeypatch.setenv("SF4L_PROD_READONLY_URL", "postgresql://y")
    s = Settings()
    assert s.check_live_mode("moltbook") == []
    assert s.check_live_mode("gonzo") == []


# --------------------------------------------------------------------------- #
# Classification schema                                                       #
# --------------------------------------------------------------------------- #


def _valid_classification_dict() -> dict:
    return {
        "is_job_shaped": True,
        "relevance": 0.85,
        "extracted_role_title": "Senior Backend Engineer",
        "extracted_role_family": "software_engineering",
        "extracted_seniority": "senior",
        "extracted_company": "Acme",
        "extracted_geography": {"country_hint": "DE", "remote_hint": "fully_remote"},
        "has_vacancy_card_url": False,
        "vacancy_card_url": None,
        "spam_signals": [],
        "language_detected": "en",
        "reasoning": "Specific role, location, and stack named.",
        "model": "test/0.1",
        "prompt_version": "seeker-classifier-v0.1",
    }


def test_validate_payload_accepts_valid():
    data = _valid_classification_dict()
    assert validate_payload(data) == data


def test_validate_payload_accepts_json_string():
    import json
    data = _valid_classification_dict()
    assert validate_payload(json.dumps(data)) == data


def test_validate_payload_rejects_relevance_over_1():
    data = _valid_classification_dict()
    data["relevance"] = 1.5
    with pytest.raises(ClassifierSchemaError, match="schema validation failed"):
        validate_payload(data)


def test_validate_payload_rejects_missing_required():
    data = _valid_classification_dict()
    del data["reasoning"]
    with pytest.raises(ClassifierSchemaError, match="schema validation failed"):
        validate_payload(data)


def test_validate_payload_rejects_unknown_field():
    data = _valid_classification_dict()
    data["unexpected"] = "value"
    with pytest.raises(ClassifierSchemaError, match="schema validation failed"):
        validate_payload(data)


def test_validate_payload_rejects_non_json_string():
    with pytest.raises(ClassifierSchemaError, match="not valid JSON"):
        validate_payload("not json {")


def test_classification_roundtrip():
    """from_dict + to_dict must round-trip without losing fields."""
    data = _valid_classification_dict()
    c = Classification.from_dict(data)
    out = c.to_dict()
    assert validate_payload(out) == out  # round-trip stays schema-valid


# --------------------------------------------------------------------------- #
# EchoProvider determinism                                                    #
# --------------------------------------------------------------------------- #


def test_echo_provider_classifies_job_post_as_relevant():
    p = EchoProvider()
    result = p.classify(_post("We are hiring a senior backend engineer in Berlin"))
    assert result.is_job_shaped
    assert result.relevance == 0.85
    assert result.spam_signals == []


def test_echo_provider_detects_card_url():
    p = EchoProvider()
    text = "Hiring engineer — card https://kitsuno.ai/handshake/v0.1/vacancies/role-x.json"
    result = p.classify(_post(text))
    assert result.has_vacancy_card_url
    assert result.vacancy_card_url == "https://kitsuno.ai/handshake/v0.1/vacancies/role-x.json"


def test_echo_provider_flags_mlm_spam():
    p = EchoProvider()
    result = p.classify(_post("Looking for a marketing role — work from home, earn $5000 in 7 days!"))
    assert result.is_job_shaped is True  # has 'looking for' + 'role'
    assert "mlm" in result.spam_signals
    assert result.relevance == 0.4


def test_echo_provider_low_relevance_for_chatter():
    p = EchoProvider()
    result = p.classify(_post("Just sharing my thoughts on the weather"))
    assert not result.is_job_shaped
    assert result.relevance == 0.1


def test_echo_provider_output_is_schema_valid():
    """EchoProvider must emit a Classification that passes the JSON schema."""
    p = EchoProvider()
    result = p.classify(_post("hiring designer"))
    assert validate_payload(result.to_dict()) is not None


# --------------------------------------------------------------------------- #
# Gate: kill switch                                                           #
# --------------------------------------------------------------------------- #


def test_gate_kill_switch_open():
    d = gate.check_kill_switch(kill_file_exists=False)
    assert d.allowed


def test_gate_kill_switch_engaged():
    d = gate.check_kill_switch(kill_file_exists=True)
    assert not d.allowed
    assert d.gate == "kill_switch"


# --------------------------------------------------------------------------- #
# Gate: submolt allowlist                                                     #
# --------------------------------------------------------------------------- #


def test_gate_submolt_allowed():
    d = gate.check_submolt("hiring", ["hiring", "robotics"])
    assert d.allowed


def test_gate_submolt_not_in_allowlist():
    d = gate.check_submolt("trolling", ["hiring", "robotics"])
    assert not d.allowed
    assert d.gate == "submolt_allowlist"


def test_gate_submolt_empty_allowlist_denies_all():
    d = gate.check_submolt("hiring", [])
    assert not d.allowed
    assert "no submolts configured" in d.reason


# --------------------------------------------------------------------------- #
# Gate: relevance threshold                                                   #
# --------------------------------------------------------------------------- #


def _classification(relevance: float = 0.85, **overrides) -> Classification:
    return Classification(
        is_job_shaped=overrides.get("is_job_shaped", True),
        relevance=relevance,
        extracted_role_title=None,
        extracted_role_family=None,
        extracted_seniority=None,
        extracted_company=None,
        extracted_geography=Geography(None, None),
        has_vacancy_card_url=overrides.get("has_vacancy_card_url", False),
        vacancy_card_url=overrides.get("vacancy_card_url"),
        spam_signals=[],
        language_detected=None,
        reasoning="test",
        model="test",
        prompt_version="test",
    )


def test_gate_relevance_above_threshold():
    c = _classification(relevance=0.85)
    assert gate.check_relevance(c, 0.7).allowed


def test_gate_relevance_at_threshold():
    c = _classification(relevance=0.7)
    assert gate.check_relevance(c, 0.7).allowed


def test_gate_relevance_below_threshold():
    c = _classification(relevance=0.69)
    d = gate.check_relevance(c, 0.7)
    assert not d.allowed
    assert d.gate == "relevance_threshold"


# --------------------------------------------------------------------------- #
# Gate: card URL allowlist                                                    #
# --------------------------------------------------------------------------- #


_ALLOWLIST_RE = r"^https://(?:kitsuno\.ai/handshake/v0\.1/vacancies/|app\.kitsuno\.ai/handshake/v0\.2/cards/)[a-z0-9-]+\.json$"


def test_gate_card_url_accepts_valid():
    d = gate.check_card_url(
        "https://kitsuno.ai/handshake/v0.1/vacancies/role-x.json", _ALLOWLIST_RE
    )
    assert d.allowed


def test_gate_card_url_accepts_v0_2_card():
    """S316 ea1300dc: v0.2 cards on app.kitsuno.ai/handshake/v0.2/cards/ must allow."""
    d = gate.check_card_url(
        "https://app.kitsuno.ai/handshake/v0.2/cards/toast-principal-technical-writer-e3a324a4.json",
        _ALLOWLIST_RE,
    )
    assert d.allowed


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        # Wrong host
        "https://evil.example/handshake/v0.1/vacancies/role-x.json",
        # http not https
        "http://kitsuno.ai/handshake/v0.1/vacancies/role-x.json",
        # Wrong path prefix
        "https://kitsuno.ai/jobs/role-x.json",
        # Wrong version
        "https://kitsuno.ai/handshake/v0.2/vacancies/role-x.json",
        # Uppercase in slug (allowlist forces lowercase)
        "https://kitsuno.ai/handshake/v0.1/vacancies/Role-X.json",
        # Extra path segment
        "https://kitsuno.ai/handshake/v0.1/vacancies/sub/role.json",
        # Path traversal
        "https://kitsuno.ai/handshake/v0.1/vacancies/../evil.json",
        # Querystring
        "https://kitsuno.ai/handshake/v0.1/vacancies/role.json?evil=1",
        # No .json
        "https://kitsuno.ai/handshake/v0.1/vacancies/role",
        # v0.2 on wrong host (v0.2 lives on app.kitsuno.ai, not kitsuno.ai)
        "https://kitsuno.ai/handshake/v0.2/cards/role-x.json",
        # v0.2 on wrong path (cards/, not vacancies/)
        "https://app.kitsuno.ai/handshake/v0.2/vacancies/role-x.json",
        # v0.1 on wrong host (v0.1 lives on kitsuno.ai, not app.kitsuno.ai)
        "https://app.kitsuno.ai/handshake/v0.1/vacancies/role-x.json",
        # Crossed: v0.1 path on v0.2 surface
        "https://kitsuno.ai/handshake/v0.1/cards/role-x.json",
        # v0.2 uppercase in slug
        "https://app.kitsuno.ai/handshake/v0.2/cards/Role-X.json",
    ],
)
def test_gate_card_url_rejects_bad(url):
    d = gate.check_card_url(url, _ALLOWLIST_RE)
    assert not d.allowed
    assert d.gate == "card_url_allowlist"


# --------------------------------------------------------------------------- #
# Gate: card schema validity                                                  #
# --------------------------------------------------------------------------- #


class _StubValidationResult:
    def __init__(self, ok, errors=None):
        self.ok = ok
        self.errors = errors or []


def _good_validator(_card):
    return _StubValidationResult(ok=True)


def _bad_validator(_card):
    return _StubValidationResult(ok=False, errors=["missing required field 'vacancy'"])


def test_gate_card_schema_valid_accepts():
    d = gate.check_card_schema_valid({"kitso.handshake.v1": {}}, _good_validator)
    assert d.allowed


def test_gate_card_schema_valid_rejects_invalid_card():
    d = gate.check_card_schema_valid({"junk": True}, _bad_validator)
    assert not d.allowed
    assert d.gate == "card_schema_valid"
    assert "missing required field" in d.reason


def test_gate_card_schema_valid_rejects_none():
    d = gate.check_card_schema_valid(None, _good_validator)
    assert not d.allowed
    assert "None" in d.reason


# --------------------------------------------------------------------------- #
# Gate: dedup                                                                 #
# --------------------------------------------------------------------------- #


def test_gate_dedup_first_time_allowed():
    d = gate.check_card_not_seen("https://kitsuno.ai/handshake/v0.1/vacancies/x.json", set())
    assert d.allowed


def test_gate_dedup_repeat_denied():
    seen = {"https://kitsuno.ai/handshake/v0.1/vacancies/x.json"}
    d = gate.check_card_not_seen("https://kitsuno.ai/handshake/v0.1/vacancies/x.json", seen)
    assert not d.allowed
    assert d.gate == "card_already_seen"


# --------------------------------------------------------------------------- #
# Gate: field-note family                                                     #
# --------------------------------------------------------------------------- #


def test_gate_field_note_disabled_by_default():
    d = gate.check_field_note_feature_flag(False)
    assert not d.allowed
    assert d.gate == "field_note_disabled"


def test_gate_field_note_enabled():
    d = gate.check_field_note_feature_flag(True)
    assert d.allowed


def test_gate_field_note_length_under_limit():
    assert gate.check_field_note_length("short note", max_chars=280).allowed


def test_gate_field_note_length_over_limit():
    d = gate.check_field_note_length("x" * 281, max_chars=280)
    assert not d.allowed
    assert d.gate == "field_note_length"


def test_gate_field_note_rate_limit_first_post():
    d = gate.check_field_note_rate_limit(
        last_post_at=None, now=datetime.now(timezone.utc), min_interval_hours=24
    )
    assert d.allowed


def test_gate_field_note_rate_limit_too_soon():
    now = datetime.now(timezone.utc)
    last = now - timedelta(hours=2)
    d = gate.check_field_note_rate_limit(last, now, min_interval_hours=24)
    assert not d.allowed
    assert d.gate == "field_note_rate_limit"


def test_gate_field_note_rate_limit_after_window():
    now = datetime.now(timezone.utc)
    last = now - timedelta(hours=25)
    d = gate.check_field_note_rate_limit(last, now, min_interval_hours=24)
    assert d.allowed


def test_gate_field_note_rate_limit_rejects_naive_datetime():
    now = datetime.now(timezone.utc)
    naive = datetime.now()  # no tz
    d = gate.check_field_note_rate_limit(naive, now, min_interval_hours=24)
    assert not d.allowed
    assert "naive" in d.reason


def test_gate_field_note_pii_allows_clean():
    d = gate.check_field_note_pii("clean text", pii_check_fn=lambda t: True)
    assert d.allowed


def test_gate_field_note_pii_blocks_flagged():
    d = gate.check_field_note_pii("dirty", pii_check_fn=lambda t: False)
    assert not d.allowed
    assert d.gate == "field_note_pii_check"


# --------------------------------------------------------------------------- #
# Orchestrator end-to-end (dry-run)                                           #
# --------------------------------------------------------------------------- #


def test_run_tick_dry_run_moltbook(monkeypatch, tmp_path, capsys):
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("TICK_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("SEEKER_KILL_FILE", str(tmp_path / "seeker.kill"))
    settings = Settings()

    posts = [
        _post("Hiring senior engineer in Berlin"),
        _post(
            "We're looking for a designer — card "
            "https://kitsuno.ai/handshake/v0.1/vacancies/designer.json",
            post_id="p2",
        ),
        _post("Just chatting about coffee", post_id="p3"),
    ]
    rc = run_tick("moltbook", settings, posts, dry_run=True, force_echo=True)
    assert rc == 0

    err = capsys.readouterr().err
    # Three classifications + one tick_complete event
    assert err.count("\"event\": \"post_classified\"") == 3
    assert "tick_complete" in err
    # The designer post had a card URL → would_handshake outcome
    assert "would_handshake" in err
    # The chatter post dropped on relevance threshold
    assert "dropped_at_gate" in err
    assert "relevance_threshold" in err


def test_run_tick_dry_run_gonzo_no_handshake_attempt(monkeypatch, tmp_path, capsys):
    """Gonzo arm classifies but never reaches the handshake path."""
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("TICK_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("SEEKER_KILL_FILE", str(tmp_path / "seeker.kill"))
    settings = Settings()

    posts = [
        _post(
            "Hiring designer — card https://kitsuno.ai/handshake/v0.1/vacancies/x.json",
            venue="gonzo_hn_whoshiring",
            submolt="gonzo_hn_whoshiring",
        ),
    ]
    rc = run_tick("gonzo", settings, posts, dry_run=True, force_echo=True)
    assert rc == 0
    err = capsys.readouterr().err
    assert "measured_only" in err
    assert "would_handshake" not in err


def test_run_tick_kill_switch_aborts(monkeypatch, tmp_path, capsys):
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("TICK_LOCK_DIR", str(tmp_path))
    kill = tmp_path / "seeker.kill"
    kill.write_text("stop")
    monkeypatch.setenv("SEEKER_KILL_FILE", str(kill))
    settings = Settings()

    rc = run_tick("moltbook", settings, [_post("hiring")], dry_run=True, force_echo=True)
    assert rc == 6
    err = capsys.readouterr().err
    assert "tick_aborted" in err


def test_run_tick_live_mode_refuses_missing_creds(monkeypatch, tmp_path, capsys):
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("TICK_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("SEEKER_KILL_FILE", str(tmp_path / "seeker.kill"))
    settings = Settings()

    rc = run_tick("moltbook", settings, [_post("hiring")], dry_run=False)
    assert rc == 3
    err = capsys.readouterr().err
    assert "live_refused_missing_env" in err
    assert "MISTRAL_API_KEY" in err


def test_run_tick_lock_contention(monkeypatch, tmp_path, capsys):
    """Two ticks against the same arm conflict via the lock file."""
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("TICK_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("SEEKER_KILL_FILE", str(tmp_path / "seeker.kill"))
    settings = Settings()

    # Pre-create a stale lock
    (tmp_path / "seeker_moltbook.lock").write_text("99999")

    rc = run_tick("moltbook", settings, [_post("hiring")], dry_run=True, force_echo=True)
    assert rc == 7
    err = capsys.readouterr().err
    assert "tick_lock_contention" in err


# --------------------------------------------------------------------------- #
# Audit module                                                                #
# --------------------------------------------------------------------------- #


def test_audit_emit_to_stderr_when_no_db(capsys):
    audit.emit({"event": "ping"}, db=None)
    err = capsys.readouterr().err
    assert err.startswith("AUDIT-EVENT ")
    assert "\"event\": \"ping\"" in err
    assert "\"timestamp\"" in err
