"""handshake-validator -- reference v0.2 L2 -> L3-eligible quality gate.

The validator sits at one specific position in the handshake state machine:
after both sides have exchanged L2 disclosures, before either side surfaces
the conversation to a human for L3 release. Its job is to decide whether the
match is strong enough that asking a human to look at it respects the
human's time.

Public API:

    Verdict                 -- frozen dataclass: bucket + reason + dimensions
    HandshakeValidator      -- abstract base class (subclass to plug in your model)
    RuleBasedValidator      -- deterministic reference implementation
    DEFAULT_LOW_SIGNAL_CHARS -- vacancy.description length below which results
                                are stamped low_signal=true

The protocol does NOT mandate any particular classifier. It mandates the
*contract*: a (seeker_card, vacancy_card) pair in, a Verdict out. The
RuleBasedValidator is one implementation, intentionally simple, so any
operator can:

  1. Run the protocol end-to-end without an LLM,
  2. Use it as a baseline to grade their own classifier against,
  3. Inspect every decision (no model opacity).

For an LLM-backed reference shape, see examples/llm_validator_template.py.

Spec: https://kitsuno.ai/handshake/v0.2/#validator
"""
from ._version import __version__
from .verdict import (
    DEFAULT_LOW_SIGNAL_CHARS,
    FitDimension,
    FitVerdict,
    Verdict,
)
from .validator import HandshakeValidator
from .rule_based import RuleBasedValidator

__all__ = [
    "__version__",
    "DEFAULT_LOW_SIGNAL_CHARS",
    "FitDimension",
    "FitVerdict",
    "Verdict",
    "HandshakeValidator",
    "RuleBasedValidator",
]
