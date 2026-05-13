"""S298 tests for CLI subcommands (halt / resume / status) and the
credentials-file path for mistral_api_key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from seeker_agent.config import Settings, _load_seeker_credentials_file
from seeker_agent.main import cli, cmd_halt, cmd_resume


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _clean_seeker_env(monkeypatch):
    for var in (
        "SEEKER_LLM_PROVIDER",
        "MISTRAL_API_KEY",
        "SF4L_PROD_READONLY_URL",
        "SEEKER_CONNECT_MODE",
        "EXPERIMENT_DB_URL",
        "SEEKER_KILL_FILE",
        "CREDENTIALS_FILE",
        "TICK_LOCK_DIR",
        "SEEKER_KILL_TOKEN",
        "MOLTBOOK_API_KEY",
        "MOLTBOOK_ALLOWED_SUBMOLTS",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_creds(path: Path, payload: dict, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload))
    path.chmod(mode)
    return path


# --------------------------------------------------------------------------- #
# Credentials loader: mistral_api_key path                                    #
# --------------------------------------------------------------------------- #


def test_credentials_loader_surfaces_mistral_key(tmp_path):
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"mistral_api_key": "k-from-file"},
    )
    result = _load_seeker_credentials_file(creds_path=creds_file)
    assert result is not None
    assert result["mistral_api_key"] == "k-from-file"


def test_credentials_loader_returns_none_when_only_unknown_keys(tmp_path):
    """A file with only ``role`` (no URL, no mistral key) is useless."""
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"role": "seeker_ro"},
    )
    assert _load_seeker_credentials_file(creds_path=creds_file) is None


def test_settings_load_uses_mistral_key_from_file(tmp_path, monkeypatch):
    _clean_seeker_env(monkeypatch)
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"mistral_api_key": "key-from-file"},
    )
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))
    s = Settings.load()
    assert s.mistral_api_key == "key-from-file"


def test_settings_load_env_overrides_file_for_mistral_key(tmp_path, monkeypatch):
    _clean_seeker_env(monkeypatch)
    creds_file = _write_creds(
        tmp_path / "credentials.json",
        {"mistral_api_key": "from-file"},
    )
    monkeypatch.setenv("CREDENTIALS_FILE", str(creds_file))
    monkeypatch.setenv("MISTRAL_API_KEY", "from-env")
    s = Settings.load()
    assert s.mistral_api_key == "from-env"


# --------------------------------------------------------------------------- #
# halt / resume / status                                                      #
# --------------------------------------------------------------------------- #


def test_halt_creates_kill_file(tmp_path, monkeypatch, capsys):
    _clean_seeker_env(monkeypatch)
    kill_file = tmp_path / "seeker.kill"
    monkeypatch.setenv("SEEKER_KILL_FILE", str(kill_file))

    rc = cli(["halt"])
    assert rc == 0
    assert kill_file.exists()
    content = kill_file.read_text()
    assert content.startswith("halted_at=")
    assert "pid=" in content
    assert "user=" in content


def test_halt_records_reason(tmp_path, monkeypatch, capsys):
    _clean_seeker_env(monkeypatch)
    kill_file = tmp_path / "seeker.kill"
    monkeypatch.setenv("SEEKER_KILL_FILE", str(kill_file))

    rc = cli(["halt", "--reason", "mistral_quota_exhausted"])
    assert rc == 0
    content = kill_file.read_text()
    assert "reason=mistral_quota_exhausted" in content


def test_halt_idempotent_refreshes_timestamp(tmp_path, monkeypatch):
    _clean_seeker_env(monkeypatch)
    kill_file = tmp_path / "seeker.kill"
    monkeypatch.setenv("SEEKER_KILL_FILE", str(kill_file))

    cli(["halt"])
    first = kill_file.read_text()
    # Halt again — different mtime, same file
    cli(["halt", "--reason", "second-halt"])
    second = kill_file.read_text()
    assert "reason=second-halt" in second
    assert first != second


def test_resume_removes_kill_file(tmp_path, monkeypatch):
    _clean_seeker_env(monkeypatch)
    kill_file = tmp_path / "seeker.kill"
    monkeypatch.setenv("SEEKER_KILL_FILE", str(kill_file))

    cli(["halt"])
    assert kill_file.exists()
    rc = cli(["resume"])
    assert rc == 0
    assert not kill_file.exists()


def test_resume_idempotent_when_already_disengaged(tmp_path, monkeypatch):
    _clean_seeker_env(monkeypatch)
    kill_file = tmp_path / "seeker.kill"
    monkeypatch.setenv("SEEKER_KILL_FILE", str(kill_file))

    rc = cli(["resume"])
    assert rc == 0
    assert not kill_file.exists()


def test_halt_makes_subsequent_tick_abort(tmp_path, monkeypatch):
    """Engage the switch, then verify a tick refuses to run."""
    _clean_seeker_env(monkeypatch)
    kill_file = tmp_path / "seeker.kill"
    monkeypatch.setenv("SEEKER_KILL_FILE", str(kill_file))
    monkeypatch.setenv("TICK_LOCK_DIR", str(tmp_path))

    cli(["halt"])
    assert kill_file.exists()

    # Now run a tick — should return 6 (kill_switch_engaged)
    from seeker_agent.classifier import PostRecord
    from seeker_agent.main import run_tick

    settings = Settings.load()
    rc = run_tick(
        arm="gonzo",
        settings=settings,
        posts=[PostRecord(
            venue="gonzo_test", post_id="p1", post_text="x",
            observed_at="2026-05-13T00:00:00+00:00",
        )],
        dry_run=True,
        force_echo=True,
    )
    assert rc == 6


def test_status_runs_without_db(tmp_path, monkeypatch, capsys):
    """Status command should run without crashing when experiment_db_url is unset."""
    _clean_seeker_env(monkeypatch)
    monkeypatch.setenv("SEEKER_KILL_FILE", str(tmp_path / "kill"))
    # Point CREDENTIALS_FILE at a non-existent path so Settings.load() can't fill
    # experiment_db_url from the real credentials file on disk.
    monkeypatch.setenv("CREDENTIALS_FILE", str(tmp_path / "no-such-file.json"))

    rc = cli(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Seeker Agent status" in out
    assert "kill_switch_state:" in out
    assert "disengaged" in out
    assert "NOT configured" in out  # experiment_db
