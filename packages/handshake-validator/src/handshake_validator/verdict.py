"""Verdict types — the public contract returned by every validator.

The protocol mandates three buckets, four fit dimensions, and a low-signal
flag. Anything else (confidence levels, scoring breakdowns, model-specific
metadata) is implementation-defined and lives in subclass-specific extras.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping


# Vacancy descriptions shorter than this are stamped low_signal=True.
# Distinguishes "weak fit because thin data" from "weak fit because actually weak."
DEFAULT_LOW_SIGNAL_CHARS = 800


class FitVerdict(str, Enum):
    """The three-bucket routing decision.

    Only ``STRONG`` advances the conversation to L3-eligible. ``WEAK`` and
    ``NO_FIT`` both result in silent drop (no notification to either side);
    the row persists for analytics.
    """

    STRONG = "strong_fit"
    WEAK = "weak_fit"
    NO_FIT = "no_fit"


class FitDimension(str, Enum):
    """Per-dimension fit assessment, structured rationale behind the verdict."""

    MATCH = "match"
    PARTIAL = "partial"
    MISS = "miss"


@dataclass(frozen=True)
class Verdict:
    """Result of a single validator call.

    Attributes
    ----------
    verdict:
        The routing decision. Only ``FitVerdict.STRONG`` advances the
        conversation to L3-eligible.
    reason:
        One sentence, candidate-facing tone, <=200 chars. Surfaces on the
        seeker's pipeline card if (and only if) the verdict is STRONG.
        For WEAK/NO_FIT the reason is stored for analytics but not shown.
    fit_dimensions:
        Per-dimension structured assessment over the four protocol
        dimensions: ``role_alignment``, ``seniority_fit``, ``skill_overlap``,
        ``context_fit``. Each value is a :class:`FitDimension`.
    low_signal:
        ``True`` when the input data is too thin to classify reliably
        (vacancy description shorter than ``DEFAULT_LOW_SIGNAL_CHARS``).
        Distinguishes thin-data weak from actually-weak.
    extras:
        Implementation-defined extras (confidence, latency, model name,
        scoring breakdowns). The protocol does not constrain this dict.

    Notes
    -----
    Verdict instances are frozen and hashable. ``reason`` is truncated and
    HTML-stripped at construction (defensive: validator output is treated
    as input-tainted before storage/rendering).
    """

    verdict: FitVerdict
    reason: str = ""
    fit_dimensions: Mapping[str, FitDimension] = field(default_factory=dict)
    low_signal: bool = False
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cleaned = _sanitize_reason(self.reason)
        if cleaned != self.reason:
            # frozen dataclass — bypass setattr restriction
            object.__setattr__(self, "reason", cleaned)
        if not isinstance(self.verdict, FitVerdict):
            object.__setattr__(self, "verdict", FitVerdict(self.verdict))

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-compatible dict.

        Useful for persisting in a ``handshake_conversations`` row or sending
        across an A2A boundary. Matches the JSON shape documented in the
        spec under §validator.
        """
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "fit_dimensions": {
                k: (v.value if isinstance(v, FitDimension) else v)
                for k, v in self.fit_dimensions.items()
            },
            "low_signal": self.low_signal,
            "extras": dict(self.extras),
        }


def _sanitize_reason(text: str, max_chars: int = 200) -> str:
    """Strip control chars, collapse whitespace, truncate.

    Validator output is treated as input-tainted: the reason ends up rendered
    in a candidate-facing UI and must not allow injection through model output.
    No HTML, no newlines, no tabs, no length blow-out.
    """
    if not text:
        return ""
    # strip control chars and normalise whitespace
    out = []
    for ch in text:
        if ch == "\n" or ch == "\r" or ch == "\t":
            out.append(" ")
        elif ord(ch) < 0x20:
            continue
        else:
            out.append(ch)
    cleaned = "".join(out)
    # drop angle brackets defensively (no HTML in candidate-facing text)
    cleaned = cleaned.replace("<", "").replace(">", "")
    # collapse runs of whitespace
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip() + "\u2026"
    return cleaned
