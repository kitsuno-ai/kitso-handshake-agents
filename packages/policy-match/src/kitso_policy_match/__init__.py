"""kitso-policy-match — reference v0.2 handshake policy evaluator.

A pure-function, stdlib-only evaluator for the Kitso Handshake v0.2 protocol.
Extracted from the Kitsuno production stack so any party building a counter-
agent can drop it in and evaluate vacancy↔seeker matches under the published
spec without re-implementing the operator semantics from scratch.

Spec: https://kitsuno.ai/handshake/v0.2/  (see "The protocol: three disclosure tiers")

Basic use:

    from kitso_policy_match import evaluate, FIRE_L1, BLOCK_L1, BLOCK_L2

    result = evaluate(
        card_policy={"criteria": [
            {"field": "languages", "operator": "any",
             "values": ["python"], "gate": "hard"}
        ]},
        card_traits={"languages": ["python", "go"]},
        seeker_policy={"criteria": []},
        seeker_traits={"languages": ["python", "go"]},
        stage="L1",
    )

    if result["outcome"] == FIRE_L1:
        # safe to fire L1 against this vacancy
        ...

Outcomes:
    FIRE_L1       — handshake may proceed (all hard gates pass at L1)
    BLOCK_L1      — at least one hard gate fails at L1
    ELIGIBLE_L2   — internal intermediate (rarely surfaced)
    BLOCK_L2      — fails when re-evaluated under L2 strictness

Stage semantics:
    stage="L1"  — only hard mismatches block; soft mismatches recorded but pass
    stage="L2"  — both hard AND soft mismatches block

This module has zero external dependencies. Apache 2.0.
"""

from .policy_match import (
    evaluate,
    FIRE_L1,
    BLOCK_L1,
    BLOCK_L2,
    ELIGIBLE_L2,
    MATCH,
    MISMATCH,
    UNKNOWN,
    VALID_GATES,
)

__version__ = "0.2.0"
__all__ = [
    "evaluate",
    "FIRE_L1",
    "BLOCK_L1",
    "BLOCK_L2",
    "ELIGIBLE_L2",
    "MATCH",
    "MISMATCH",
    "UNKNOWN",
    "VALID_GATES",
]
