"""kitso-state-hash — reference v0.2 state_hash for Kitso Handshake.

Public API:
    vacancy_state_hash(card: dict) -> str   # 64-char lowercase hex SHA-256
    seeker_state_hash(card: dict) -> str    # 64-char lowercase hex SHA-256
    canonical_subset_vacancy(card: dict) -> dict
    canonical_subset_seeker(card: dict) -> dict
    canonical_bytes(obj) -> bytes           # raw JCS bytes for arbitrary obj

Spec: see ../../docs/s351-pair-state-idempotency-spec.md (Kitsuno) or the
Spec repo (kitsuno-ai/kitso-handshake) for the public-facing version.

Algorithm summary:
  state_hash = lowercase_hex( SHA256( JCS(canonical_subset(card)) ) )
"""
from ._version import __version__
from .canonical import (
    canonical_subset_seeker,
    canonical_subset_vacancy,
    seeker_state_hash,
    vacancy_state_hash,
)
from .jcs import canonical_bytes

__all__ = [
    "__version__",
    "vacancy_state_hash",
    "seeker_state_hash",
    "canonical_subset_vacancy",
    "canonical_subset_seeker",
    "canonical_bytes",
]
