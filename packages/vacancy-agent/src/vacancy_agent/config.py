"""Configuration for the Vacancy Agent.

All settings are loaded from environment variables. The agent fails closed
if any required variable is missing (or, in dry-run mode, only AUDIT_DB_URL is
required — everything else is optional).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Vacancy Agent settings, sourced from environment variables.

    In dry-run mode, only AUDIT_DB_URL (optional) and CARD_HOST_BASE / JD_HOST_BASE
    (optional) are read. Live mode additionally requires MOLTBOOK_API_KEY and
    AGENT_KILL_TOKEN.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore unknown env vars; never fail on extra noise
    )

    # Venue
    venue: Literal["moltbook"] = "moltbook"
    moltbook_api_base: HttpUrl = Field(default="https://api.moltbook.com/v1/")
    moltbook_api_key: str | None = Field(default=None, description="Required for live mode.")

    # Identity (declared, not enforced — the venue enforces via API key)
    agent_dns: str = Field(
        default="kitsuno.agent",
        description="Agent DNS namespace, used in audit log and post signature.",
    )
    agent_handle: str = Field(
        default="kitsuno_jobs",
        description="Public agent handle, used in post body for human-readable attribution.",
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
