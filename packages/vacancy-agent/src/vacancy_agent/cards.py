"""Vacancy card loading and validation.

Reads a vacancy-agent-card.json file from disk, fetches the official v0.1
schema from kitsuno.ai (cached), and validates the card. Returns a typed
result; never throws on a bad card — the caller decides what to do.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from jsonschema import Draft202012Validator
from jsonschema.validators import RefResolver

log = logging.getLogger(__name__)

SCHEMA_BASE = "https://kitsuno.ai/handshake/v0.1/"
SCHEMA_FILES = (
    "common.json",
    "vacancy-agent-card.json",
    "seeker-agent-card.json",
    "invitation.json",
    "disclosure.json",
)


@dataclass
class CardValidation:
    ok: bool
    errors: list[str]
    card: dict[str, Any] | None  # the parsed card if JSON parsing succeeded, else None


def _load_schemas() -> tuple[dict, dict[str, Any]]:
    """Load all v0.1 schemas from the official endpoint.

    Returns (vacancy_schema, full_store_for_resolver).
    """
    store: dict[str, Any] = {}
    for name in SCHEMA_FILES:
        url = SCHEMA_BASE + name
        with urlopen(url, timeout=10) as resp:  # noqa: S310 — well-known URL
            schema = json.load(resp)
        store[schema["$id"]] = schema
    return store[SCHEMA_BASE + "vacancy-agent-card.json"], store


# Module-level cache: schemas are immutable per v0.1
_SCHEMA_CACHE: tuple[dict, dict[str, Any]] | None = None


def _schemas() -> tuple[dict, dict[str, Any]]:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = _load_schemas()
    return _SCHEMA_CACHE


def validate_card(card_path: Path | str) -> CardValidation:
    """Load and validate a vacancy card against the Kitso Handshake v0.1 schema.

    Args:
        card_path: filesystem path to the card JSON

    Returns:
        CardValidation with ok=True if the card is schema-compliant, ok=False otherwise.
        On any error (file not found, invalid JSON, schema fetch failure), returns
        ok=False with a descriptive error message — never raises.
    """
    path = Path(card_path)
    if not path.is_file():
        return CardValidation(ok=False, errors=[f"File not found: {path}"], card=None)

    try:
        with path.open() as fp:
            card = json.load(fp)
    except json.JSONDecodeError as exc:
        return CardValidation(ok=False, errors=[f"Invalid JSON: {exc}"], card=None)

    try:
        vacancy_schema, store = _schemas()
    except Exception as exc:  # noqa: BLE001 — schema fetch genuinely can fail in many ways
        return CardValidation(
            ok=False,
            errors=[f"Schema fetch failed: {exc}"],
            card=card,
        )

    resolver = RefResolver(
        base_uri=vacancy_schema["$id"],
        referrer=vacancy_schema,
        store=store,
    )
    validator = Draft202012Validator(vacancy_schema, resolver=resolver)
    errors = sorted(validator.iter_errors(card), key=lambda e: list(e.absolute_path))
    if not errors:
        return CardValidation(ok=True, errors=[], card=card)

    formatted = []
    for err in errors:
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        formatted.append(f"[{loc}] {err.message}")
    return CardValidation(ok=False, errors=formatted, card=card)


def extract_post_summary(card: dict[str, Any]) -> dict[str, str]:
    """Pull the human-readable summary fields from a card for the post body.

    The card itself is agent-facing; the post body is human-and-agent-facing.
    This function bridges the two without adding fields the schema forbids.
    """
    kh = card["kitso.handshake.v1"]
    vac = kh["vacancy"]
    entity = kh.get("hiring_entity", {})

    summary = {
        "role_title": vac["role_title"],
        "role_family": vac["role_family"],
        "seniority": vac.get("seniority", ""),
        "employment_type": vac["employment_type"],
        "country": vac.get("geography", {}).get("country", ""),
        "remote_policy": vac.get("geography", {}).get("remote_policy", ""),
        "hiring_entity": entity.get("name", "") if entity.get("name_disclosed_in_invitation") else "",
    }
    return summary
