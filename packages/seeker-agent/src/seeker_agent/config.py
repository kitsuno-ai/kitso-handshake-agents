"""Configuration for the Seeker Agent.

All settings are loaded from environment variables. The agent runs in two
arms (moltbook + gonzo) which can be enabled independently; either arm can
be active for dry-run without LLM credentials, but a live run requires the
provider's credentials and the kill token.

Per the design doc §14 (resolved S295), the agent uses **free tier only**:
Mistral primary, Cloudflare Workers AI as failover. ``SEEKER_LLM_PROVIDER``
is the switch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

LLMProvider = Literal["mistral", "cloudflare"]
ArmName = Literal["moltbook", "gonzo"]


class Settings(BaseSettings):
    """Seeker Agent settings, sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM provider ----------------------------------------------------- #

    seeker_llm_provider: LLMProvider = Field(
        default="mistral",
        description="Which LLM provider the classifier calls. Both must be free tier.",
    )
    mistral_api_key: str | None = Field(default=None)
    mistral_model: str = Field(
        default="open-mistral-nemo",  # free-tier-capable, JSON-mode-capable
        description="Mistral model id. Override if Mistral changes free-tier offerings.",
    )
    cloudflare_api_token: str | None = Field(default=None)
    cloudflare_account_id: str | None = Field(default=None)
    cloudflare_model: str = Field(
        default="@cf/qwen/qwen1.5-14b-chat-awq",
        description="Cloudflare Workers AI model slug. Tune as CF catalog evolves.",
    )

    # --- Classifier behavior --------------------------------------------- #

    seeker_relevance_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Below this, all further verbs are gated.",
    )
    classifier_temperature: float = Field(default=0.1, ge=0.0, le=1.0)
    classifier_timeout_seconds: float = Field(default=30.0, gt=0.0)
    classifier_prompt_version: str = Field(default="seeker-classifier-v0.1")
    classifier_max_post_chars: int = Field(
        default=8000,
        description="Truncate longer posts before sending to LLM. Audit excerpt is 500 chars.",
    )

    # --- Moltbook arm ----------------------------------------------------- #

    moltbook_arm_enabled: bool = Field(default=True)
    moltbook_api_base: str = Field(default="https://www.moltbook.com/api/v1/")
    moltbook_api_key: str | None = Field(default=None)
    moltbook_allowed_submolts: str = Field(
        default="",
        description=(
            "Comma-separated submolt names the seeker is permitted to poll. "
            "Empty = arm refuses to fetch. Greg locks the list near kickoff."
        ),
    )

    # --- Gonzo arm -------------------------------------------------------- #

    gonzo_arm_enabled: bool = Field(default=True)
    sf4l_prod_readonly_url: str | None = Field(
        default=None,
        description=(
            "Postgres URL for read-only access to sf4l_prod (gonzo_* market_data only). "
            "Required for the gonzo arm in live mode."
        ),
    )
    gonzo_channels: str = Field(
        default=(
            "gonzo_hn_whoshiring,gonzo_bluesky,gonzo_telegram,"
            "gonzo_reddit,gonzo_lobsters_whoshiring,gonzo_mastodon"
        ),
        description="Comma-separated source values matched in market_data.source.",
    )

    # --- Card allowlist + handshake -------------------------------------- #

    card_url_allowlist_regex: str = Field(
        default=r"^https://kitsuno\.ai/handshake/v0\.1/vacancies/[a-z0-9-]+\.json$",
        description=(
            "Card URLs proposed by the classifier must match this exact pattern "
            "or the gate drops them. v1 = our own cards only."
        ),
    )
    card_fetch_timeout_seconds: float = Field(default=10.0, gt=0.0)

    # --- Field note (disabled in v1 per design §14.4) -------------------- #

    field_note_enabled: bool = Field(default=False)
    field_note_min_interval_hours: int = Field(default=24, ge=1)
    field_note_max_chars: int = Field(default=280)
    field_note_target_submolt: str = Field(default="")  # set if field notes are turned on

    # --- Persistence ----------------------------------------------------- #

    experiment_db_url: str | None = Field(
        default=None,
        description="Postgres URL for the isolated experiment DB. If unset, audit -> stdout.",
    )

    # --- Kill switch ----------------------------------------------------- #

    seeker_kill_file: Path = Field(default=Path("/tmp/seeker.kill"))
    seeker_kill_token: str | None = Field(default=None)

    # --- Rate / cadence -------------------------------------------------- #

    tick_lock_dir: Path = Field(default=Path("/tmp"))

    # ===================================================================== #

    def submolt_list(self) -> list[str]:
        """Parse MOLTBOOK_ALLOWED_SUBMOLTS into a clean list."""
        return [s.strip() for s in self.moltbook_allowed_submolts.split(",") if s.strip()]

    def gonzo_channel_list(self) -> list[str]:
        """Parse GONZO_CHANNELS into a clean list."""
        return [s.strip() for s in self.gonzo_channels.split(",") if s.strip()]

    def llm_credentials_ok(self) -> tuple[bool, list[str]]:
        """Whether the configured provider has credentials. Returns (ok, missing)."""
        if self.seeker_llm_provider == "mistral":
            return (
                self.mistral_api_key is not None,
                [] if self.mistral_api_key else ["MISTRAL_API_KEY"],
            )
        if self.seeker_llm_provider == "cloudflare":
            missing = []
            if not self.cloudflare_api_token:
                missing.append("CLOUDFLARE_API_TOKEN")
            if not self.cloudflare_account_id:
                missing.append("CLOUDFLARE_ACCOUNT_ID")
            return (not missing, missing)
        return (False, [f"unknown_provider:{self.seeker_llm_provider}"])

    def check_live_mode(self, arm: ArmName) -> list[str]:
        """Return missing env vars that would prevent live operation of `arm`.

        Empty list means live mode is OK for that arm.
        """
        missing: list[str] = []

        # Shared: LLM credentials + kill token
        ok, llm_missing = self.llm_credentials_ok()
        if not ok:
            missing.extend(llm_missing)
        if not self.seeker_kill_token:
            missing.append("SEEKER_KILL_TOKEN")

        # Per-arm
        if arm == "moltbook":
            if not self.moltbook_api_key:
                missing.append("MOLTBOOK_API_KEY")
            if not self.submolt_list():
                missing.append("MOLTBOOK_ALLOWED_SUBMOLTS")
        elif arm == "gonzo":
            if not self.sf4l_prod_readonly_url:
                missing.append("SF4L_PROD_READONLY_URL")

        # Persistence is required in live mode (no point running a measurement
        # arm with stdout-only audit)
        if not self.experiment_db_url:
            missing.append("EXPERIMENT_DB_URL")

        return missing
