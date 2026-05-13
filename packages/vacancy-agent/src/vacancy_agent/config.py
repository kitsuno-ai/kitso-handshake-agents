"""Configuration for the Vacancy Agent.

All settings are loaded from environment variables. The agent fails closed
if any required variable is missing (or, in dry-run mode, only AUDIT_DB_URL is
required — everything else is optional).

Credentials (specifically MOLTBOOK_API_KEY) can also be sourced from a JSON
file at $HOME/.config/moltbook/credentials.json — see _load_handle_credentials
and Settings.load() below. The env var, when set, always wins.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


def _load_handle_credentials(
    creds_path: Path | None = None,
    handle: str = "kitsuno_jobs",
) -> dict | None:
    """Load credentials for a named handle from a JSON file.

    Default path: ``$HOME/.config/moltbook/credentials.json``.

    The expected file shape is a top-level object keyed by agent handle, with
    each value carrying at minimum an ``api_key`` field. Other fields (
    ``verification_code``, ``claim_url``, ``profile_url``, ...) are preserved
    for callers that want them but are ignored here.

    Returns the per-handle dict, or ``None`` if the file is missing or the
    handle is not present. Raises ``PermissionError`` if the file exists but
    has unsafe permissions (any group- or other-readable bits set) — credentials
    must not be world- or group-visible.
    """
    if creds_path is None:
        home = Path(os.environ.get("HOME") or Path.home())
        creds_path = home / ".config" / "moltbook" / "credentials.json"
    creds_path = Path(creds_path)

    if not creds_path.exists():
        return None

    st = creds_path.stat()
    # Reject if any group or other permission bits are set (mask 0o077).
    if st.st_mode & 0o077:
        raise PermissionError(
            f"{creds_path} has mode {oct(stat.S_IMODE(st.st_mode))}; "
            f"must be 0600 or stricter. Run: chmod 600 {creds_path}"
        )

    with creds_path.open() as f:
        data = json.load(f)

    if not isinstance(data, dict):
        log.warning("credentials file %s is not a JSON object; ignoring", creds_path)
        return None

    handle_block = data.get(handle)
    if not isinstance(handle_block, dict):
        return None
    return handle_block


class Settings(BaseSettings):
    """Vacancy Agent settings, sourced from environment variables.

    In dry-run mode, only AUDIT_DB_URL (optional) and CARD_HOST_BASE / JD_HOST_BASE
    (optional) are read. Live mode additionally requires MOLTBOOK_API_KEY and
    AGENT_KILL_TOKEN.

    Construct with :meth:`Settings.load` to also consult the credentials file
    fallback for MOLTBOOK_API_KEY. Plain ``Settings()`` reads only environment.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore unknown env vars; never fail on extra noise
    )

    # Venue
    venue: Literal["moltbook"] = "moltbook"
    moltbook_api_base: HttpUrl = Field(default="https://www.moltbook.com/api/v1/")
    moltbook_api_key: str | None = Field(default=None, description="Required for live mode.")

    # Identity (declared, not enforced — the venue enforces via API key)
    agent_dns: str = Field(
        default="kitsuno.agent",
        description="Agent DNS namespace, used in audit log and post signature.",
    )
    agent_handle: str = Field(
        default="kitsuno_jobs",
        description=(
            "Public agent handle, used in post body for human-readable attribution "
            "AND as the lookup key into the credentials file. Override via "
            "AGENT_HANDLE env var to switch handles (e.g. kitsuno_seeks)."
        ),
    )

    # Credentials file (optional fallback for MOLTBOOK_API_KEY)
    credentials_file: Path | None = Field(
        default=None,
        description=(
            "Path to JSON credentials file. If unset, Settings.load() falls back "
            "to $HOME/.config/moltbook/credentials.json. The env var "
            "MOLTBOOK_API_KEY always wins over this file."
        ),
    )

    # Hosting (the URLs of the JSON cards and human-readable JDs referenced from posts)
    card_host_base: HttpUrl = Field(
        default="https://kitsuno.ai/handshake/v0.1/vacancies/",
        description="Base URL where vacancy card JSON files are served.",
    )
    jd_host_base: HttpUrl = Field(
        default="https://kitsuno.ai/jobs/",
        description="Base URL where human-readable JD pages are served.",
    )

    # Audit
    audit_db_url: str | None = Field(
        default=None,
        description="Postgres URL for the audit DB. If unset, audit events go to stdout.",
    )

    # Kill switch
    agent_kill_token: str | None = Field(
        default=None,
        description="Random token authenticating /kill requests. Required in live mode.",
    )

    # AUP defaults — DO NOT raise these in a fork without explicit venue permission
    rate_limit_seconds_between_posts: int = Field(
        default=1800,  # 30 minutes per Moltbook AUP
        description="Minimum seconds between posts on the same agent. Venue AUP.",
    )

    def check_live_mode(self) -> list[str]:
        """Return a list of missing required env vars for live mode.

        An empty list means live mode is OK; a non-empty list means the agent
        MUST refuse to run live (call sys.exit(1) at the entry point).
        """
        missing = []
        if not self.moltbook_api_key:
            missing.append("MOLTBOOK_API_KEY")
        if not self.agent_kill_token:
            missing.append("AGENT_KILL_TOKEN")
        return missing

    @classmethod
    def load(cls) -> "Settings":
        """Construct Settings and enrich ``moltbook_api_key`` from the credentials
        file when the env var is absent.

        Environment variables always win. The credentials file is consulted only
        when ``MOLTBOOK_API_KEY`` is not in the environment. The lookup key into
        the file is ``agent_handle`` (default ``kitsuno_jobs``, overridable via
        ``AGENT_HANDLE`` env var).

        Raises ``PermissionError`` if the credentials file exists but has unsafe
        permissions; the agent must refuse to run in that case.
        """
        s = cls()
        if s.moltbook_api_key is None:
            creds = _load_handle_credentials(
                creds_path=s.credentials_file,
                handle=s.agent_handle,
            )
            if creds and creds.get("api_key"):
                s = s.model_copy(update={"moltbook_api_key": creds["api_key"]})
                log.info(
                    "moltbook_api_key loaded from credentials file for handle=%s",
                    s.agent_handle,
                )
        return s
