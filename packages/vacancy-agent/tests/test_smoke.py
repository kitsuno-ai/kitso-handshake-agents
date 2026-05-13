"""Smoke tests for the Vacancy Agent.

These tests do NOT call the venue API. They verify:
- A valid card validates
- An invalid card fails validation
- The post body extracts the right fields
- The CLI refuses to run live without credentials
- The Moltbook client composes the correct post URL
- The credentials-file loader reads handle blocks, refuses unsafe permissions,
  and is safely no-op when the file is absent
- Settings.load() prefers the env var but falls back to the credentials file
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vacancy_agent.cards import extract_post_summary, validate_card
from vacancy_agent.config import Settings, _load_handle_credentials
from vacancy_agent.main import _format_post_body, _slug_from_card_path
from vacancy_agent.moltbook_client import MoltbookClient

REPO_ROOT = Path(__file__).resolve().parents[3]
VALID_FIXTURES = REPO_ROOT / "test-fixtures" / "valid"
INVALID_FIXTURES = REPO_ROOT / "test-fixtures" / "invalid"


def _load(path: Path) -> dict:
    with path.open() as fp:
        return json.load(fp)


# --------------------------------------------------------------------------- #
# Card validation                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fixture_name",
    [
        "vacancy-card-direct-hire.json",
        "vacancy-card-rpo.json",
    ],
)
def test_valid_fixtures_validate(fixture_name):
    """Every fixture in test-fixtures/valid/ must validate against the v0.1 schema."""
    result = validate_card(VALID_FIXTURES / fixture_name)
    assert result.ok, f"Expected {fixture_name} to validate. Errors: {result.errors}"


def test_invalid_fixture_fails_validation():
    """An obviously-bad card should fail validation, not silently pass."""
    result = validate_card(INVALID_FIXTURES / "vacancy-card-missing-required.json")
    assert not result.ok
    assert len(result.errors) > 0


def test_missing_file_returns_error_not_raise():
    """validate_card must never raise; it returns ok=False with an explanation."""
    result = validate_card("/tmp/definitely-does-not-exist-12345.json")
    assert not result.ok
    assert any("not found" in e.lower() for e in result.errors)


# --------------------------------------------------------------------------- #
# Post body construction                                                      #
# --------------------------------------------------------------------------- #


def test_post_summary_extracts_fields():
    card = _load(VALID_FIXTURES / "vacancy-card-direct-hire.json")
    summary = extract_post_summary(card)
    assert summary["role_title"] == card["kitso.handshake.v1"]["vacancy"]["role_title"]
    assert summary["employment_type"] == card["kitso.handshake.v1"]["vacancy"]["employment_type"]


def test_format_post_body_includes_links():
    card = _load(VALID_FIXTURES / "vacancy-card-direct-hire.json")
    title, content = _format_post_body(
        card,
        card_url="https://example.com/handshake/v0.1/vacancies/role.json",
        jd_url="https://example.com/jobs/role",
    )
    assert len(title) <= 120
    assert "Kitso Handshake v0.1 card" in content
    assert "https://example.com/handshake/v0.1/vacancies/role.json" in content
    assert "https://example.com/jobs/role" in content


def test_slug_from_card_path():
    assert _slug_from_card_path(Path("/tmp/social-media-specialist.json")) == "social-media-specialist"
    assert _slug_from_card_path(Path("foo/bar/agentic-ai-developer.json")) == "agentic-ai-developer"


# --------------------------------------------------------------------------- #
# Settings live-mode check                                                    #
# --------------------------------------------------------------------------- #


def test_settings_fails_closed_for_live_mode():
    """If MOLTBOOK_API_KEY is unset, check_live_mode must surface it."""
    settings = Settings(
        moltbook_api_key=None,
        agent_kill_token=None,
    )
    missing = settings.check_live_mode()
    assert "MOLTBOOK_API_KEY" in missing
    assert "AGENT_KILL_TOKEN" in missing


# --------------------------------------------------------------------------- #
# Moltbook base URL                                                           #
# --------------------------------------------------------------------------- #


def test_settings_moltbook_api_base_default(monkeypatch):
    """Default base URL must be www.moltbook.com/api/v1/, not api.moltbook.com/v1/."""
    monkeypatch.delenv("MOLTBOOK_API_BASE", raising=False)
    s = Settings()
    assert str(s.moltbook_api_base) == "https://www.moltbook.com/api/v1/"


def test_moltbook_client_default_post_url():
    """The client's resolved post URL must be the correct www endpoint."""
    client = MoltbookClient(api_key="fake-test-key")
    assert client.post_url == "https://www.moltbook.com/api/v1/posts"


def test_moltbook_client_post_url_normalises_trailing_slash():
    """Whether the api_base ends in / or not, the post URL is the same."""
    a = MoltbookClient(api_key="k", api_base="https://www.moltbook.com/api/v1/")
    b = MoltbookClient(api_key="k", api_base="https://www.moltbook.com/api/v1")
    assert a.post_url == b.post_url == "https://www.moltbook.com/api/v1/posts"


def test_moltbook_client_refuses_empty_api_key():
    with pytest.raises(ValueError, match="api_key is required"):
        MoltbookClient(api_key="")


# --------------------------------------------------------------------------- #
# Credentials file loader                                                     #
# --------------------------------------------------------------------------- #


def _write_creds(path: Path, payload: dict, mode: int = 0o600) -> Path:
    """Write a credentials JSON file with the given mode and return its path."""
    path.write_text(json.dumps(payload))
    path.chmod(mode)
    return path


def test_load_credentials_returns_handle_block(tmp_path):
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {
            "kitsuno_jobs": {"api_key": "JOBS_KEY", "verification_code": "ok-1"},
            "kitsuno_seeks": {"api_key": "SEEKS_KEY"},
        },
    )
    block = _load_handle_credentials(creds_path=creds_file, handle="kitsuno_jobs")
    assert block is not None
    assert block["api_key"] == "JOBS_KEY"
    assert block["verification_code"] == "ok-1"


def test_load_credentials_picks_correct_handle(tmp_path):
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {
            "kitsuno_jobs": {"api_key": "JOBS_KEY"},
            "kitsuno_seeks": {"api_key": "SEEKS_KEY"},
        },
    )
    block = _load_handle_credentials(creds_path=creds_file, handle="kitsuno_seeks")
    assert block is not None
    assert block["api_key"] == "SEEKS_KEY"


def test_load_credentials_missing_handle_returns_none(tmp_path):
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"kitsuno_jobs": {"api_key": "X"}},
    )
    assert _load_handle_credentials(creds_path=creds_file, handle="nonexistent") is None


def test_load_credentials_missing_file_returns_none(tmp_path):
    """Missing file is a no-op, not an error — fresh checkouts must not break."""
    assert (
        _load_handle_credentials(
            creds_path=tmp_path / "does-not-exist.json",
            handle="kitsuno_jobs",
        )
        is None
    )


def test_load_credentials_refuses_group_readable(tmp_path):
    creds_file = _write_creds(tmp_path / "credentials.json", {"kitsuno_jobs": {}}, mode=0o640)
    with pytest.raises(PermissionError, match="0600 or stricter"):
        _load_handle_credentials(creds_path=creds_file, handle="kitsuno_jobs")


def test_load_credentials_refuses_world_readable(tmp_path):
    creds_file = _write_creds(tmp_path / "credentials.json", {"kitsuno_jobs": {}}, mode=0o644)
    with pytest.raises(PermissionError, match="0600 or stricter"):
        _load_handle_credentials(creds_path=creds_file, handle="kitsuno_jobs")


def test_load_credentials_handle_block_not_object_returns_none(tmp_path):
    """If the handle key maps to a non-object (e.g. a string), treat as absent."""
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"kitsuno_jobs": "not-an-object"},
    )
    assert _load_handle_credentials(creds_path=creds_file, handle="kitsuno_jobs") is None


def test_load_credentials_default_path_uses_home(tmp_path, monkeypatch):
    """When creds_path is None, the loader uses $HOME/.config/moltbook/credentials.json."""
    fake_home = tmp_path / "home"
    moltbook_dir = fake_home / ".config" / "moltbook"
    moltbook_dir.mkdir(parents=True)
    _write_creds(
        moltbook_dir / "credentials.json",
        {"kitsuno_jobs": {"api_key": "FROM_HOME"}},
    )
    monkeypatch.setenv("HOME", str(fake_home))
    block = _load_handle_credentials(creds_path=None, handle="kitsuno_jobs")
    assert block is not None
    assert block["api_key"] == "FROM_HOME"


# --------------------------------------------------------------------------- #
# Settings.load() — env vs file precedence                                    #
# --------------------------------------------------------------------------- #


def _clean_settings_env(monkeypatch):
    """Strip every Settings field name from the env so .load() reads from defaults."""
    for var in (
        "MOLTBOOK_API_KEY",
        "MOLTBOOK_API_BASE",
        "AGENT_HANDLE",
        "AGENT_KILL_TOKEN",
        "AUDIT_DB_URL",
        "CREDENTIALS_FILE",
        "CARD_HOST_BASE",
        "JD_HOST_BASE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_settings_load_uses_file_when_env_missing(tmp_path, monkeypatch):
    _clean_settings_env(monkeypatch)
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"kitsuno_jobs": {"api_key": "FROM_FILE"}},
    )
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))

    s = Settings.load()
    assert s.moltbook_api_key == "FROM_FILE"


def test_settings_load_env_overrides_file(tmp_path, monkeypatch):
    _clean_settings_env(monkeypatch)
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"kitsuno_jobs": {"api_key": "FROM_FILE"}},
    )
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))
    monkeypatch.setenv("MOLTBOOK_API_KEY", "FROM_ENV")

    s = Settings.load()
    assert s.moltbook_api_key == "FROM_ENV"


def test_settings_load_respects_agent_handle(tmp_path, monkeypatch):
    _clean_settings_env(monkeypatch)
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {
            "kitsuno_jobs": {"api_key": "JOBS_KEY"},
            "kitsuno_seeks": {"api_key": "SEEKS_KEY"},
        },
    )
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))
    monkeypatch.setenv("AGENT_HANDLE", "kitsuno_seeks")

    s = Settings.load()
    assert s.agent_handle == "kitsuno_seeks"
    assert s.moltbook_api_key == "SEEKS_KEY"


def test_settings_load_missing_file_leaves_key_none(tmp_path, monkeypatch):
    """When file is absent and env unset, api_key stays None and live mode refuses."""
    _clean_settings_env(monkeypatch)
    monkeypatch.setenv("CREDENTIALS_FILE", str(tmp_path / "nonexistent.json"))

    s = Settings.load()
    assert s.moltbook_api_key is None
    assert "MOLTBOOK_API_KEY" in s.check_live_mode()


def test_settings_load_raises_on_bad_perms(tmp_path, monkeypatch):
    """A world-readable credentials file must surface as PermissionError."""
    _clean_settings_env(monkeypatch)
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"kitsuno_jobs": {"api_key": "X"}},
        mode=0o644,
    )
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))

    with pytest.raises(PermissionError):
        Settings.load()
