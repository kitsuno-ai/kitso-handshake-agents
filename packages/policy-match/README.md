# kitso-policy-match

> Reference v0.2 handshake policy evaluator for the [Kitso Handshake](https://kitsuno.ai/handshake/v0.2/) protocol.

A pure-function, stdlib-only evaluator for the L1/L2 disclosure-tier gating that the v0.2 spec defines. Extracted from Kitsuno's production stack and published as Apache 2.0 reference so any party building a counter-agent can evaluate vacancy↔seeker matches under the published spec without re-implementing the operator semantics from scratch.

**Status:** v0.2.0 — tracks the v0.2 draft of the Kitso Handshake spec.

## Install

```bash
pip install -e packages/policy-match
```

(Not yet on PyPI; vendoring works fine — the module has zero external dependencies.)

## Usage

```python
from kitso_policy_match import evaluate, FIRE_L1, BLOCK_L1

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
```

## Operators

The closed operator set defined by v0.2 §5.3:

| Operator | Semantics |
|---|---|
| `equals` | exact equality |
| `in` | criterion value list contains trait |
| `not_in` | criterion value list does not contain trait |
| `any` | criterion values intersect trait values (set semantics) |
| `all` | criterion values are a subset of trait values |
| `gte` | trait ≥ criterion value |
| `lte` | trait ≤ criterion value |
| `present` | trait is declared (non-empty) |
| `absent` | trait is not declared, or declared empty |

## Gate semantics

| Gate | At L1 | At L2 |
|---|---|---|
| `hard` | blocks | blocks |
| `soft` | recorded, does not block | blocks |
| `informational` | recorded, does not block | recorded, does not block |

## Outcomes

- `FIRE_L1` — handshake may proceed at L1 (all hard gates pass)
- `BLOCK_L1` — at least one hard gate fails at L1
- `ELIGIBLE_L2` — internal intermediate (rarely surfaced)
- `BLOCK_L2` — fails when re-evaluated under L2 strictness (hard or soft mismatch)

The evaluator runs *symmetrically* — both sides' criteria are checked against the other side's traits. A criterion the other side has not declared a value for evaluates to `unknown`; hard-gate unknowns block at L1 (don't fire on uncertainty); soft-gate unknowns block at L2 only.

## Tests

```bash
pip install -e packages/policy-match[dev]
pytest packages/policy-match/tests
```

## License

Apache License 2.0. See [`LICENSE`](../../LICENSE) at the repo root.
