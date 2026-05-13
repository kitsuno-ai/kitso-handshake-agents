"""S296 additions on top of the S295 smoke suite.

Coverage:
- Seeker credentials file loader (parallel to vacancy-agent pattern)
- Settings.load() factory with file fallback
- fetch_next_gonzo_batch — channel allowlist, SQL params, row mapping
- MistralProvider — auth check, JSON parsing, schema validation, rate limit
- Provider factory + orchestrator failure recovery

The S295 suite (60 tests, in tests/test_smoke.py) continues to exercise
classifier schema, EchoProvider, gates, and orchestrator basics. This file
adds the new wiring; it does NOT redo the S295 coverage.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from seeker_agent.classifier import (
    Classification,
    ClassifierSchemaError,
    PostRecord,
)
from seeker_agent.config import Settings, _load_seeker_credentials_file
from seeker_agent.main import run_tick
from seeker_agent.providers.mistral import MistralError, MistralProvider
from seeker_agent.verbs import (
    GONZO_CHANNELS_ALLOWED,
    _fetch_gonzo_rows,
    _row_to_post_record,
    fetch_next_gonzo_batch,
)


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _clean_seeker_env(monkeypatch):
    for var in (
        "SEEKER_LLM_PROVIDER",
        "MISTRAL_API_KEY",
        "MISTRAL_API_BASE",
        "MISTRAL_MODEL",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "SEEKER_RELEVANCE_THRESHOLD",
        "MOLTBOOK_ARM_ENABLED",
        "MOLTBOOK_API_KEY",
        "MOLTBOOK_ALLOWED_SUBMOLTS",
        "GONZO_ARM_ENABLED",
        "SF4L_PROD_READONLY_URL",
        "SEEKER_CONNECT_MODE",
        "GONZO_CHANNELS",
        "FIELD_NOTE_ENABLED",
        "EXPERIMENT_DB_URL",
        "SEEKER_KILL_TOKEN",
        "SEEKER_KILL_FILE",
        "TICK_LOCK_DIR",
        "CREDENTIALS_FILE",
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


def _write_creds(path: Path, payload: dict, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload))
    path.chmod(mode)
    return path


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


# --------------------------------------------------------------------------- #
# S296: Seeker credentials file loader                                        #
# --------------------------------------------------------------------------- #


def test_load_seeker_credentials_returns_host_url(tmp_path):
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {
            "sf4l_prod_readonly_url_host": "postgresql://h@127.0.0.1:5434/db",
            "sf4l_prod_readonly_url_internal": "postgresql://h@pg:5432/db",
            "role": "seeker_ro",
        },
    )
    result = _load_seeker_credentials_file(creds_path=creds_file, prefer="host")
    assert result is not None
    assert result["sf4l_prod_readonly_url"] == "postgresql://h@127.0.0.1:5434/db"
    assert result["role"] == "seeker_ro"


def test_load_seeker_credentials_returns_internal_url(tmp_path):
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {
            "sf4l_prod_readonly_url_host": "postgresql://h@127.0.0.1:5434/db",
            "sf4l_prod_readonly_url_internal": "postgresql://h@pg:5432/db",
        },
    )
    result = _load_seeker_credentials_file(creds_path=creds_file, prefer="internal")
    assert result is not None
    assert result["sf4l_prod_readonly_url"] == "postgresql://h@pg:5432/db"


def test_load_seeker_credentials_missing_file_returns_none(tmp_path):
    assert _load_seeker_credentials_file(creds_path=tmp_path / "nope.json") is None


def test_load_seeker_credentials_refuses_group_readable(tmp_path):
    creds_file = _write_creds(tmp_path / "credentials.json", {}, mode=0o640)
    with pytest.raises(PermissionError, match="0600 or stricter"):
        _load_seeker_credentials_file(creds_path=creds_file)


def test_load_seeker_credentials_refuses_world_readable(tmp_path):
    creds_file = _write_creds(tmp_path / "credentials.json", {}, mode=0o644)
    with pytest.raises(PermissionError, match="0600 or stricter"):
        _load_seeker_credentials_file(creds_path=creds_file)


def test_load_seeker_credentials_handles_missing_key(tmp_path):
    """File exists but lacks the host URL key — returns None, not crash."""
    creds_file = _write_creds(tmp_path / "credentials.json", {"role": "seeker_ro"})
    assert _load_seeker_credentials_file(creds_path=creds_file, prefer="host") is None


# --------------------------------------------------------------------------- #
# S296: Settings.load() factory                                               #
# --------------------------------------------------------------------------- #


def test_settings_load_uses_file_when_env_missing(tmp_path, monkeypatch):
    _clean_seeker_env(monkeypatch)
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"sf4l_prod_readonly_url_host": "postgresql://from_file"},
    )
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))
    s = Settings.load()
    assert s.sf4l_prod_readonly_url == "postgresql://from_file"


def test_settings_load_env_overrides_file(tmp_path, monkeypatch):
    _clean_seeker_env(monkeypatch)
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"sf4l_prod_readonly_url_host": "postgresql://from_file"},
    )
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))
    monkeypatch.setenv("SF4L_PROD_READONLY_URL", "postgresql://from_env")
    s = Settings.load()
    assert s.sf4l_prod_readonly_url == "postgresql://from_env"


def test_settings_load_respects_connect_mode(tmp_path, monkeypatch):
    _clean_seeker_env(monkeypatch)
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {
            "sf4l_prod_readonly_url_host": "postgresql://h",
            "sf4l_prod_readonly_url_internal": "postgresql://i",
        },
    )
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))
    monkeypatch.setenv("SEEKER_CONNECT_MODE", "internal")
    s = Settings.load()
    assert s.sf4l_prod_readonly_url == "postgresql://i"


def test_settings_load_raises_on_bad_perms(tmp_path, monkeypatch):
    _clean_seeker_env(monkeypatch)
    creds_file = _write_creds(tmp_path / "credentials.json", {}, mode=0o644)
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))
    with pytest.raises(PermissionError):
        Settings.load()


# --------------------------------------------------------------------------- #
# S296: fetch_next_gonzo_batch                                                #
# --------------------------------------------------------------------------- #


def test_gonzo_channels_allowed_matches_design():
    assert GONZO_CHANNELS_ALLOWED == frozenset({
        "gonzo_hn_whoshiring",
        "gonzo_bluesky",
        "gonzo_telegram",
        "gonzo_reddit",
        "gonzo_lobsters_whoshiring",
        "gonzo_mastodon",
    })


def test_fetch_next_gonzo_batch_rejects_unknown_channel():
    with pytest.raises(ValueError, match="not in GONZO_CHANNELS_ALLOWED"):
        fetch_next_gonzo_batch(
            channel="not_a_real_channel",
            since=None,
            limit=10,
            sf4l_prod_readonly_url="postgresql://x",
        )


def test_row_to_post_record_basic():
    row = {
        "post_id": "abc-123",
        "venue": "gonzo_hn_whoshiring",
        "post_title": "Senior Engineer",
        "post_text": "We are hiring...",
        "language_hint": None,
        "observed_at": datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
    }
    pr = _row_to_post_record(row)
    assert pr.venue == "gonzo_hn_whoshiring"
    assert pr.post_id == "abc-123"
    assert pr.post_text == "We are hiring..."
    assert pr.post_title == "Senior Engineer"
    assert pr.observed_at == "2026-05-13T09:00:00+00:00"
    assert pr.submolt_or_channel == "gonzo_hn_whoshiring"


def test_row_to_post_record_handles_naive_datetime():
    """gonzo_first_seen is timestamptz so should always have tz, but be defensive."""
    row = {
        "post_id": "abc",
        "venue": "gonzo_bluesky",
        "post_title": None,
        "post_text": "x",
        "language_hint": "",
        "observed_at": datetime(2026, 5, 13, 9, 0),  # naive
    }
    pr = _row_to_post_record(row)
    assert pr.observed_at  # non-empty string


def test_row_to_post_record_empty_post_text_becomes_empty_string():
    row = {
        "post_id": "abc",
        "venue": "gonzo_bluesky",
        "post_title": "t",
        "post_text": None,
        "language_hint": None,
        "observed_at": datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
    }
    pr = _row_to_post_record(row)
    assert pr.post_text == ""


class _FakeCursor:
    """psycopg2-shaped cursor with a context-manager protocol."""

    def __init__(self, rows):
        self._rows = rows
        self.last_sql = None
        self.last_params = None

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class _FakeConn:
    """Minimal psycopg2-shaped connection that returns a configured cursor."""

    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self, cursor_factory=None):
        # ignore cursor_factory — our fake returns dicts directly
        return self.cursor_obj


def test_fetch_gonzo_rows_uses_parameterized_query():
    rows = [{
        "post_id": "p1", "venue": "gonzo_hn_whoshiring",
        "post_title": "t", "post_text": "x", "language_hint": None,
        "observed_at": datetime(2026, 5, 13, tzinfo=timezone.utc),
    }]
    conn = _FakeConn(rows)
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    result = _fetch_gonzo_rows(conn, "gonzo_hn_whoshiring", since, 50)

    assert len(result) == 1
    assert result[0]["post_id"] == "p1"
    # SQL must be parameterized (no string concat of channel into SQL)
    assert "gonzo_hn_whoshiring" not in conn.cursor_obj.last_sql
    assert "%(channel)s" in conn.cursor_obj.last_sql
    # Params dict has channel + since + limit
    assert conn.cursor_obj.last_params == {
        "channel": "gonzo_hn_whoshiring",
        "since": since,
        "limit": 50,
    }


def test_fetch_gonzo_rows_rejects_unknown_channel():
    conn = _FakeConn([])
    with pytest.raises(ValueError):
        _fetch_gonzo_rows(conn, "definitely_not_real", None, 10)


def test_fetch_gonzo_rows_handles_no_since():
    conn = _FakeConn([])
    _fetch_gonzo_rows(conn, "gonzo_bluesky", None, 10)
    assert conn.cursor_obj.last_params["since"] is None


# --------------------------------------------------------------------------- #
# S296: MistralProvider                                                       #
# --------------------------------------------------------------------------- #


def _mistral_response(content: str) -> dict:
    """Wrap content in Mistral's chat-completions response shape."""
    return {
        "id": "test-id",
        "object": "chat.completion",
        "model": "mistral-small-latest",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def test_mistral_provider_refuses_empty_api_key():
    with pytest.raises(ValueError, match="api_key is required"):
        MistralProvider(api_key="")


def test_mistral_provider_successful_classification(httpx_mock):
    """Happy path: API returns valid JSON-mode response, parsed + schema-validated."""
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        json=_mistral_response(json.dumps(_valid_classification_dict())),
    )
    provider = MistralProvider(api_key="test-key", min_gap_seconds=0)
    result = provider.classify(_post("hiring senior engineer"))
    assert isinstance(result, Classification)
    assert result.is_job_shaped is True
    assert result.relevance == 0.85
    assert result.prompt_version == "seeker-classifier-v0.2"


def test_mistral_provider_strips_markdown_fences(httpx_mock):
    """Defensive: if model wraps JSON in ```json fences, we still parse it."""
    content = f"```json\n{json.dumps(_valid_classification_dict())}\n```"
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        json=_mistral_response(content),
    )
    provider = MistralProvider(api_key="k", min_gap_seconds=0)
    result = provider.classify(_post("x"))
    assert result.is_job_shaped is True


def test_mistral_provider_overrides_hallucinated_model_field(httpx_mock):
    """Even if the LLM puts a wrong model name in its JSON, we overwrite with the real one."""
    payload = _valid_classification_dict()
    payload["model"] = "claude-9-ultra"  # hallucinated
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        json=_mistral_response(json.dumps(payload)),
    )
    provider = MistralProvider(api_key="k", min_gap_seconds=0)
    result = provider.classify(_post("x"))
    assert "mistral" in result.model.lower()


def test_mistral_provider_raises_on_schema_invalid_response(httpx_mock):
    bad = _valid_classification_dict()
    bad["relevance"] = 1.5  # out of range
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        json=_mistral_response(json.dumps(bad)),
    )
    provider = MistralProvider(api_key="k", min_gap_seconds=0)
    with pytest.raises(ClassifierSchemaError):
        provider.classify(_post("x"))


def test_mistral_provider_raises_on_non_json_response(httpx_mock):
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        json=_mistral_response("not valid json at all"),
    )
    provider = MistralProvider(api_key="k", min_gap_seconds=0)
    with pytest.raises(ClassifierSchemaError):
        provider.classify(_post("x"))


def test_mistral_provider_raises_on_5xx(httpx_mock):
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        status_code=500,
        text="upstream error",
    )
    provider = MistralProvider(api_key="k", min_gap_seconds=0)
    with pytest.raises(MistralError, match="500"):
        provider.classify(_post("x"))


def test_mistral_provider_paces_between_calls(httpx_mock):
    """Two back-to-back calls should be separated by >= min_gap_seconds."""
    body = _mistral_response(json.dumps(_valid_classification_dict()))
    httpx_mock.add_response(url="https://api.mistral.ai/v1/chat/completions", json=body)
    httpx_mock.add_response(url="https://api.mistral.ai/v1/chat/completions", json=body)

    provider = MistralProvider(api_key="k", min_gap_seconds=0.2)
    t0 = time.monotonic()
    provider.classify(_post("x", post_id="a"))
    provider.classify(_post("x", post_id="b"))
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.2, f"pacing not enforced; elapsed={elapsed:.3f}s"


def test_mistral_provider_request_includes_json_mode(httpx_mock):
    """Verify the outgoing request asks for JSON mode."""
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        json=_mistral_response(json.dumps(_valid_classification_dict())),
    )
    provider = MistralProvider(api_key="k", min_gap_seconds=0)
    provider.classify(_post("x"))

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["model"] == "mistral-small-latest"
    assert payload["temperature"] == 0.1
    # System + user
    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["role"] == "system"
    assert "UNTRUSTED_CONTENT" in payload["messages"][1]["content"]


def test_mistral_provider_classification_passes_through_orchestrator(httpx_mock, monkeypatch, tmp_path):
    """End-to-end: real provider (mocked HTTP) runs through run_tick."""
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("TICK_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("SEEKER_KILL_FILE", str(tmp_path / "seeker.kill"))
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setenv("MISTRAL_MIN_GAP_SECONDS", "0")

    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        json=_mistral_response(json.dumps(_valid_classification_dict())),
    )

    settings = Settings()
    rc = run_tick("gonzo", settings, [_post("hiring senior engineer", venue="gonzo_hn_whoshiring", submolt="gonzo_hn_whoshiring")], dry_run=True)
    assert rc == 0


# --------------------------------------------------------------------------- #
# S296: Orchestrator with MistralProvider failure recovery                    #
# --------------------------------------------------------------------------- #


def test_run_tick_handles_provider_exception(monkeypatch, tmp_path, capsys, httpx_mock):
    """A provider call that raises mid-loop should be logged and skipped, not abort the tick."""
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("TICK_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("SEEKER_KILL_FILE", str(tmp_path / "seeker.kill"))
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setenv("MISTRAL_MIN_GAP_SECONDS", "0")

    # First call: 500. Second call: success.
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions", status_code=500
    )
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        json=_mistral_response(json.dumps(_valid_classification_dict())),
    )

    settings = Settings()
    posts = [
        _post("hiring 1", venue="gonzo_hn_whoshiring", post_id="a", submolt="gonzo_hn_whoshiring"),
        _post("hiring 2", venue="gonzo_hn_whoshiring", post_id="b", submolt="gonzo_hn_whoshiring"),
    ]
    rc = run_tick("gonzo", settings, posts, dry_run=True)
    assert rc == 0  # tick succeeded despite the per-post failure

    err = capsys.readouterr().err
    assert "classifier_call_failed" in err
    # tick_complete should still fire
    assert "tick_complete" in err


# --------------------------------------------------------------------------- #
# S296: Provider factory                                                      #
# --------------------------------------------------------------------------- #


def test_build_provider_force_echo(monkeypatch):
    _clean_seeker_env(monkeypatch)
    from seeker_agent.main import _build_provider
    s = Settings()
    p = _build_provider(s, force_echo=True)
    assert p.name.startswith("echo-")


def test_build_provider_mistral_requires_api_key(monkeypatch):
    _clean_seeker_env(monkeypatch)
    from seeker_agent.main import _build_provider
    s = Settings()
    with pytest.raises(RuntimeError, match="MISTRAL_API_KEY"):
        _build_provider(s, force_echo=False)


def test_build_provider_mistral_returns_mistral_provider(monkeypatch):
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    from seeker_agent.main import _build_provider
    s = Settings()
    p = _build_provider(s, force_echo=False)
    assert isinstance(p, MistralProvider)


def test_build_provider_cloudflare_not_yet_wired(monkeypatch):
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("SEEKER_LLM_PROVIDER", "cloudflare")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    from seeker_agent.main import _build_provider
    s = Settings()
    with pytest.raises(NotImplementedError, match="S297"):
        _build_provider(s, force_echo=False)

def test_mistral_provider_retries_on_429(httpx_mock):
    """First call: 429. Second call: 200. Provider should retry and succeed."""
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        status_code=429,
        headers={"retry-after": "1"},
        text='{"error":"rate_limited"}',
    )
    httpx_mock.add_response(
        url="https://api.mistral.ai/v1/chat/completions",
        json=_mistral_response(json.dumps(_valid_classification_dict())),
    )
    provider = MistralProvider(api_key="k", min_gap_seconds=0)
    result = provider.classify(_post("hiring"))
    assert result.is_job_shaped is True
    # Two requests made (the retry + the success)
    requests = httpx_mock.get_requests()
    assert len(requests) == 2


def test_mistral_provider_gives_up_after_3_retries(httpx_mock):
    """Four consecutive 429s should raise MistralError (we retry 3 times)."""
    for _ in range(4):
        httpx_mock.add_response(
            url="https://api.mistral.ai/v1/chat/completions",
            status_code=429,
            headers={"retry-after": "0.1"},
            text='{"error":"rate_limited"}',
        )
    provider = MistralProvider(api_key="k", min_gap_seconds=0)
    with pytest.raises(MistralError, match="429"):
        provider.classify(_post("x"))
    requests = httpx_mock.get_requests()
    assert len(requests) == 4
