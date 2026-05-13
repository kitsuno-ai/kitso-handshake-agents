"""Classifier interface and the EchoProvider used for testing.

The classifier is an LLM call that returns a strict JSON object matching the
schema in :data:`CLASSIFICATION_SCHEMA`. Real LLM integrations (Mistral,
Cloudflare Workers AI) land in S296. For S295 the orchestrator and gate
exercise the interface via :class:`EchoProvider`, which returns deterministic
output based on simple rules. This lets us test the entire flow without
network calls.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from jsonschema import Draft202012Validator

# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #

#: JSON Schema (Draft 2020-12) for classifier output. Mirrors design §4.2.
CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "SeekerClassification",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_job_shaped",
        "relevance",
        "extracted_role_title",
        "extracted_role_family",
        "extracted_seniority",
        "extracted_company",
        "extracted_geography",
        "has_vacancy_card_url",
        "vacancy_card_url",
        "spam_signals",
        "language_detected",
        "reasoning",
        "model",
        "prompt_version",
    ],
    "properties": {
        "is_job_shaped": {"type": "boolean"},
        "relevance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "extracted_role_title": {"type": ["string", "null"]},
        "extracted_role_family": {"type": ["string", "null"]},
        "extracted_seniority": {"type": ["string", "null"]},
        "extracted_company": {"type": ["string", "null"]},
        "extracted_geography": {
            "type": "object",
            "additionalProperties": False,
            "required": ["country_hint", "remote_hint"],
            "properties": {
                "country_hint": {"type": ["string", "null"]},
                "remote_hint": {"type": ["string", "null"]},
            },
        },
        "has_vacancy_card_url": {"type": "boolean"},
        "vacancy_card_url": {"type": ["string", "null"]},
        "spam_signals": {"type": "array", "items": {"type": "string"}},
        "language_detected": {"type": ["string", "null"]},
        "reasoning": {"type": "string"},
        "model": {"type": "string"},
        "prompt_version": {"type": "string"},
    },
}

_validator = Draft202012Validator(CLASSIFICATION_SCHEMA)


# --------------------------------------------------------------------------- #
# Data types                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PostRecord:
    """A single post observed at a venue, ready for classification.

    `post_text` is the only untrusted field; orchestration code wraps it in
    a fenced block when constructing the LLM prompt.
    """

    venue: str
    post_id: str
    post_text: str
    observed_at: str  # ISO 8601 UTC
    post_title: str | None = None
    submolt_or_channel: str | None = None
    language_hint: str | None = None


@dataclass(frozen=True)
class Geography:
    country_hint: str | None
    remote_hint: str | None


@dataclass
class Classification:
    """Strict-schema classifier output. Mirrors §4.2."""

    is_job_shaped: bool
    relevance: float
    extracted_role_title: str | None
    extracted_role_family: str | None
    extracted_seniority: str | None
    extracted_company: str | None
    extracted_geography: Geography
    has_vacancy_card_url: bool
    vacancy_card_url: str | None
    spam_signals: list[str]
    language_detected: str | None
    reasoning: str
    model: str
    prompt_version: str

    # Latency + cost are recorded by the orchestrator, not the LLM.
    latency_ms: int | None = field(default=None)
    cost_usd_estimate: float | None = field(default=None)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Classification":
        """Build from validated dict. Caller MUST have run :func:`validate_payload`."""
        geo_raw = data["extracted_geography"]
        return cls(
            is_job_shaped=data["is_job_shaped"],
            relevance=float(data["relevance"]),
            extracted_role_title=data["extracted_role_title"],
            extracted_role_family=data["extracted_role_family"],
            extracted_seniority=data["extracted_seniority"],
            extracted_company=data["extracted_company"],
            extracted_geography=Geography(
                country_hint=geo_raw["country_hint"],
                remote_hint=geo_raw["remote_hint"],
            ),
            has_vacancy_card_url=data["has_vacancy_card_url"],
            vacancy_card_url=data["vacancy_card_url"],
            spam_signals=list(data["spam_signals"]),
            language_detected=data["language_detected"],
            reasoning=data["reasoning"],
            model=data["model"],
            prompt_version=data["prompt_version"],
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["extracted_geography"] = {
            "country_hint": self.extracted_geography.country_hint,
            "remote_hint": self.extracted_geography.remote_hint,
        }
        # latency_ms and cost_usd_estimate are tracking fields, not schema fields
        d.pop("latency_ms", None)
        d.pop("cost_usd_estimate", None)
        return d


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #


class ClassifierSchemaError(ValueError):
    """Raised when an LLM response does not match :data:`CLASSIFICATION_SCHEMA`."""


def validate_payload(payload: Any) -> dict[str, Any]:
    """Return `payload` if it validates against the schema, else raise.

    Accepts a parsed dict or a JSON string.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ClassifierSchemaError(f"response is not valid JSON: {exc}") from exc

    errors = sorted(_validator.iter_errors(payload), key=lambda e: e.path)
    if errors:
        msgs = "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errors[:5])
        raise ClassifierSchemaError(f"schema validation failed: {msgs}")

    return payload


# --------------------------------------------------------------------------- #
# Provider protocol                                                           #
# --------------------------------------------------------------------------- #


class ClassifierProvider(Protocol):
    """Anything that turns a PostRecord into a Classification.

    Real providers (Mistral, Cloudflare) implement this in S296. For S295,
    :class:`EchoProvider` keeps the orchestrator end-to-end-testable.
    """

    name: str

    def classify(self, post: PostRecord) -> Classification: ...


# --------------------------------------------------------------------------- #
# EchoProvider — deterministic stub                                           #
# --------------------------------------------------------------------------- #


_JOB_KEYWORDS = re.compile(
    r"\b(hiring|hire|engineer|developer|designer|manager|seeking|looking for|"
    r"position|opening|role|join (our|the) team|we[\u2019']re looking)\b",
    re.IGNORECASE,
)
_CARD_URL_RE = re.compile(r"https://kitsuno\.ai/handshake/v0\.1/vacancies/[a-z0-9-]+\.json")
_MLM_SIGNALS = re.compile(r"\b(work from home|earn \$\d+|crypto pump|get rich|side hustle)\b", re.IGNORECASE)


class EchoProvider:
    """A deterministic classifier provider used for testing and dry-runs.

    Rules:
    - `is_job_shaped` is True iff the post text matches `_JOB_KEYWORDS`
    - `relevance` is 0.85 if job-shaped + no spam signals; 0.4 if job-shaped + spam; 0.1 otherwise
    - `has_vacancy_card_url` true iff a card-shaped URL appears in the text
    - Spam signals are populated from `_MLM_SIGNALS`

    This is not realistic classification — it's deterministic enough to drive
    end-to-end tests of the gate + orchestrator without an LLM in the loop.
    """

    name: str = "echo-provider/0.1"

    def __init__(self, prompt_version: str = "seeker-classifier-v0.1") -> None:
        self.prompt_version = prompt_version

    def classify(self, post: PostRecord) -> Classification:
        text = post.post_text or ""
        is_job = bool(_JOB_KEYWORDS.search(text))
        spam = sorted({"mlm"} if _MLM_SIGNALS.search(text) else set())
        if is_job and not spam:
            relevance = 0.85
        elif is_job and spam:
            relevance = 0.4
        else:
            relevance = 0.1

        card_match = _CARD_URL_RE.search(text)
        has_card = card_match is not None

        return Classification(
            is_job_shaped=is_job,
            relevance=relevance,
            extracted_role_title=None,
            extracted_role_family=None,
            extracted_seniority=None,
            extracted_company=None,
            extracted_geography=Geography(country_hint=None, remote_hint=None),
            has_vacancy_card_url=has_card,
            vacancy_card_url=card_match.group(0) if card_match else None,
            spam_signals=spam,
            language_detected=post.language_hint,
            reasoning=f"echo-provider: is_job={is_job} spam={spam} has_card={has_card}",
            model=self.name,
            prompt_version=self.prompt_version,
        )
