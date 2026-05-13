"""Configuration for the Seeker Agent.

All settings are loaded from environment variables. The agent runs in two
arms (moltbook + gonzo) which can be enabled independently; either arm can
be active for dry-run without LLM credentials, but a live run requires the
provider's credentials and the kill token.

Per the design doc §14 (resolved S295), the agent uses **free tier only**:
Mistral primary, Cloudflare Workers AI as failover. ``SEEKER_LLM_PROVIDER``
is the switch.

Credentials that don't fit in env vars (PG URLs with embedded passwords)
live in a JSON file at ``$HOME/.config/seeker/credentials.json`` (mode 0600).
See :func:`_load_seeker_credentials_file` and :meth:`Settings.load`.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

LLMProvider = Literal["mistral", "cloudflare"]
ArmName = Literal["moltbook", "gonzo"]
ConnectMode = Literal["host", "internal"]


# --------------------------------------------------------------------------- #
# Credentials file loader (parallel to vacancy-agent's pattern)              #
# --------------------------------------------------------------------------- #


def _load_seeker_credentials_file(
    creds_path: Path | None = None,
    prefer: ConnectMode = "host",
) -> dict | None:
    """Load the seeker's credentials JSON.

    Default path: ``$HOME/.config/seeker/credentials.json``. Expected shape::

        {
          "sf4l_prod_readonly_url_internal": "postgresql://seeker_ro:...@sf4l-postgres-prod:5432/sf4l_prod",
          "sf4l_prod_readonly_url_host":     "postgresql://seeker_ro:...@127.0.0.1:5434/sf4l_prod",
          "experiment_db_url_internal":      "postgresql://seeker_writer:...@experiment-db-postgres:5432/kitso_handshake_experiment",
          "experiment_db_url_host":          "postgresql://seeker_writer:...@127.0.0.1:5435/kitso_handshake_experiment",
          "role": "seeker_ro",
          "experiment_role": "seeker_writer"
        }

    Returns a dict with resolved URLs (host vs internal per ``prefer``),
    or ``None`` if the file is absent. Raises ``PermissionError`` if the
    file is world- or group-readable.

    Either URL may be absent from the file — callers should check for
    ``sf4l_prod_readonly_url`` and ``experiment_db_url`` independently.
    Returns ``None`` only when BOTH URL keys are absent (the file is then
    useless to this loader).
    """
    if creds_path is None:
        home = Path(os.environ.get("HOME") or Path.home())
        creds_path = home / ".config" / "seeker" / "credentials.json"
    creds_path = Path(creds_path)

    if not creds_path.exists():
        return None

    st = creds_path.stat()
    if st.st_mode & 0o077:
        raise PermissionError(
            f"{creds_path} has mode {oct(stat.S_IMODE(st.st_mode))}; "
            f"must be 0600 or stricter. Run: chmod 600 {creds_path}"
        )

    with creds_path.open() as f:
        data = json.load(f)

    if not isinstance(data, dict):
        log.warning("seeker credentials file %s is not a JSON object; ignoring", creds_path)
        return None

    out: dict = {}
    sf4l_key = f"sf4l_prod_readonly_url_{prefer}"
    if sf4l_key in data and isinstance(data[sf4l_key], str):
        out["sf4l_prod_readonly_url"] = data[sf4l_key]

    exp_key = f"experiment_db_url_{prefer}"
    if exp_key in data and isinstance(data[exp_key], str):
        out["experiment_db_url"] = data[exp_key]

    if not out:
        # Neither URL present — the file has no value to us
        return None

    # Pass through any other known fields
    for k in ("role", "experiment_role"):
        if k in data:
            out[k] = data[k]
    return out


# --------------------------------------------------------------------------- #
# Settings                                                                    #
# --------------------------------------------------------------------------- #


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
    mistral_api_base: str = Field(default="https://api.mistral.ai/v1")
    mistral_model: str = Field(
        default="mistral-small-latest",
        description="Mistral model id. Matches kitso_router canonical free-tier model.",
    )
    mistral_min_gap_seconds: float = Field(
        default=1.0,
        description="Free-tier rate limit: 1 req/s. Provider enforces; we pre-wait.",
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
    classifier_prompt_version: str = Field(default="seeker-classifier-v0.2")
    classifier_max_post_chars: int = Field(
        default=8000,
        description="Truncate longer posts before sending to LLM. Audit excerpt is 500 chars.",
    )

    # --- Moltbook arm ----------------------------------------------------- #

    moltbook_arm_enabled: bool = Field(default=True)
    moltbook_api_base: str = Field(default="https://www.moltbook.com/api/v1/")
    moltbook_api_key: str | None = Field(default=None)
    moltbook_allowed_submolts: str = Field(default="")

    # --- Gonzo arm -------------------------------------------------------- #

    gonzo_arm_enabled: bool = Field(default=True)
    sf4l_prod_readonly_url: str | None = Field(
        default=None,
        description="Postgres URL for read-only access to sf4l_prod. Settings.load() can fill from credentials file.",
    )
    seeker_connect_mode: ConnectMode = Field(
        default="host",
        description="Which sf4l_prod URL to use from the credentials file (host vs internal docker network).",
    )
    gonzo_channels: str = Field(
        default=(
            "gonzo_hn_whoshiring,gonzo_bluesky,gonzo_telegram,"
            "gonzo_reddit,gonzo_lobsters_whoshiring,gonzo_mastodon"
        ),
    )

    # --- Card allowlist + handshake -------------------------------------- #

    card_url_allowlist_regex: str = Field(
        default=r"^https://kitsuno\.ai/handshake/v0\.1/vacancies/[a-z0-9-]+\.json$",
    )
    card_fetch_timeout_seconds: float = Field(default=10.0, gt=0.0)

    # --- Field note (disabled in v1 per design §14.4) -------------------- #

    field_note_enabled: bool = Field(default=False)
    field_note_min_interval_hours: int = Field(default=24, ge=1)
    field_note_max_chars: int = Field(default=280)
    field_note_target_submolt: str = Field(default="")

    # --- Persistence ----------------------------------------------------- #

    experiment_db_url: str | None = Field(
        default=None,
        description="Postgres URL for the isolated experiment DB. Settings.load() can fill from credentials file.",
    )

    # --- Kill switch ----------------------------------------------------- #

    seeker_kill_file: Path = Field(default=Path("/tmp/seeker.kill"))
    seeker_kill_token: str | None = Field(default=None)

    # --- Rate / cadence -------------------------------------------------- #

    tick_lock_dir: Path = Field(default=Path("/tmp"))

    # --- Credentials file path ------------------------------------------- #

    credentials_file: Path | None = Field(
        default=None,
        description=(
            "Override path for seeker credentials JSON. "
            "Default is $HOME/.config/seeker/credentials.json."
        ),
    )

    # ===================================================================== #

    def submolt_list(self) -> list[str]:
        return [s.strip() for s in self.moltbook_allowed_submolts.split(",") if s.strip()]

    def gonzo_channel_list(self) -> list[str]:
        return [s.strip() for s in self.gonzo_channels.split(",") if s.strip()]

    def llm_credentials_ok(self) -> tuple[bool, list[str]]:
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
        missing: list[str] = []
        ok, llm_missing = self.llm_credentials_ok()
        if not ok:
            missing.extend(llm_missing)
        if not self.seeker_kill_token:
            missing.append("SEEKER_KILL_TOKEN")
        if arm == "moltbook":
            if not self.moltbook_api_key:
                missing.append("MOLTBOOK_API_KEY")
            if not self.submolt_list():
                missing.append("MOLTBOOK_ALLOWED_SUBMOLTS")
        elif arm == "gonzo":
            if not self.sf4l_prod_readonly_url:
                missing.append("SF4L_PROD_READONLY_URL")
        if not self.experiment_db_url:
            missing.append("EXPERIMENT_DB_URL")
        return missing

    @classmethod
    def load(cls) -> "Settings":
        """Construct Settings, enriching from the seeker credentials file when env is silent.

        Env vars always win. The credentials file is consulted only for keys
        not already in the environment (``SF4L_PROD_READONLY_URL`` and
        ``EXPERIMENT_DB_URL``).

        Raises ``PermissionError`` if the credentials file exists but has
        unsafe permissions; the agent must refuse to run in that case.
        """
        s = cls()
        needs_sf4l = s.sf4l_prod_readonly_url is None
        needs_exp = s.experiment_db_url is None
        if not (needs_sf4l or needs_exp):
            return s

        creds = _load_seeker_credentials_file(
            creds_path=s.credentials_file,
            prefer=s.seeker_connect_mode,
        )
        if not creds:
            return s

        updates: dict = {}
        if needs_sf4l and creds.get("sf4l_prod_readonly_url"):
            updates["sf4l_prod_readonly_url"] = creds["sf4l_prod_readonly_url"]
        if needs_exp and creds.get("experiment_db_url"):
            updates["experiment_db_url"] = creds["experiment_db_url"]

        if updates:
            log.info(
                "credentials file populated: %s (mode=%s)",
                ", ".join(sorted(updates.keys())),
                s.seeker_connect_mode,
            )
            return s.model_copy(update=updates)
        return s
