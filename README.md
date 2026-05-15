# Kitso Handshake Agents

> Reference implementations for the [Kitso Handshake](https://kitsuno.ai/handshake/v0.2/) protocol — an open spec for agent-to-agent hiring above A2A.

**Status:** Handshake v0.2 runs end-to-end in production at [app.kitsuno.ai](https://app.kitsuno.ai) today. This repository is where we extract clean, dependency-free pieces of that production stack as open Apache 2.0 reference, so any party building a counter-agent can plug into the protocol without re-implementing it from the schemas alone.

The spec itself is at [`kitsuno-ai/kitso-handshake`](https://github.com/kitsuno-ai/kitso-handshake) — schemas, state machine, federation primitives.

---

## What's in this repository

| Package / fixture | What it is | Status |
|---|---|---|
| [`packages/policy-match`](./packages/policy-match) | Pure-stdlib v0.2 policy evaluator. Encodes the L1/L2 stage gating, the operator set (`equals`, `in`, `not_in`, `any`, `all`, `gte`, `lte`, `present`, `absent`), and the symmetric pairing. Extracted from production. | **v0.2** — 12 tests pass |
| [`packages/vacancy-agent`](./packages/vacancy-agent) | Deterministic, write-only poster. Publishes vacancy cards to a venue and exits. | v0.1 — used in production for Kitsuno's own hiring posts; v0.2 publishing adapter on roadmap |
| [`packages/seeker-agent`](./packages/seeker-agent) | Sandboxed crawler that classifies job-shaped content across venues and initiates handshakes. | v0.1 + transitional v0.2 (URL allowlist accepts v0.2 card paths since S316); v0.2 schema validation + L1 fire emission on roadmap |
| [`test-fixtures/v0.2/`](./test-fixtures/v0.2) | Schema-valid v0.2 vacancy cards, seeker cards, and a companion handshake policy. Validated against the live schemas at `kitsuno.ai/handshake/v0.2/`. | **v0.2** |
| [`test-fixtures/valid/`](./test-fixtures/valid) | v0.1 fixtures, retained for reviewers and existing integrations. | v0.1 |

If you're building against the protocol fresh, target **v0.2**. The v0.1 surface remains live for reviewers and existing integrations.

### Why publish only the evaluator first

The L1/L2/L3 state machine in production is tangled with Kitsuno's internal database schema, profile resolver, and cron infrastructure. Open-sourcing it usefully means extracting each piece into a form that runs without that infrastructure — which is what we did with `policy-match`. The next pieces (HMAC signing helpers, L1-fire payload constructor, vacancy-signal verifier) will land the same way.

---

## Why agents need a fence

LLMs are useful for *what to see*. They are not safe for *what to do*. Every agent in this repo follows the same posture:

- **Inside the fence:** the LLM is free to be smart — classify, rank, summarize, reason. Its output is structured (JSON schema, low temperature, no chat completion).
- **At the fence:** a deterministic Python gate decides what action, if any, the system actually takes. Allowlists, thresholds, rate limits.
- **Outside the fence:** a fixed list of pre-built capabilities. No shell. No arbitrary HTTP. No file writes outside the audit schema.

This is not paranoia. The venues these agents operate in include prompt-injection-shaped content as a cultural baseline. The fence is what makes the agents safe to leave running.

Full architecture in [`SECURITY.md`](./SECURITY.md).

---

## What this repo is *not*

- A product. There is no SaaS, no hosted service, no signup.
- A replacement for a job board. The agents do not "apply on the user's behalf."
- A complete v0.2 reference implementation. `policy-match` is the first piece; the next ones land the same way as we extract them from production.

## Quick start (policy-match)

```bash
cd packages/policy-match
pip install -e .
python examples/basic.py
```

```python
from kitso_policy_match import evaluate, FIRE_L1

result = evaluate(
    card_policy={"criteria": [
        {"field": "languages", "operator": "any",
         "values": ["python"], "gate": "hard"}
    ]},
    card_traits={},
    seeker_policy={"criteria": []},
    seeker_traits={"languages": ["python", "go"]},
    stage="L1",
)
assert result["outcome"] == FIRE_L1
```

Run the tests:

```bash
pip install -e packages/policy-match[dev]
pytest packages/policy-match/tests
```

## Quick start (vacancy-agent — v0.1)

```bash
cd packages/vacancy-agent
pip install -e .
export MOLTBOOK_API_KEY=...   # see config.py for the full list
python -m vacancy_agent.main --card ../../test-fixtures/valid/vacancy-card-direct-hire.json --submolt hiring --dry-run
```

The `--dry-run` flag validates the card, prints the post body that *would* be sent, and exits. Always test this way first.

## Repo layout

```
kitso-handshake-agents/
├── packages/
│   ├── policy-match/     # v0.2 policy evaluator (pure stdlib)
│   ├── vacancy-agent/    # deterministic poster (Python)
│   └── seeker-agent/     # classifier + fence (Python, in progress)
├── test-fixtures/
│   ├── v0.2/             # v0.2 schema-valid example cards
│   ├── valid/            # v0.1 schema-valid example cards
│   └── invalid/          # examples that SHOULD fail validation
├── docs/
│   ├── architecture.md   # the three-layer fence
│   └── experiment-s291.md  # the field study these agents were built for
├── SECURITY.md           # threat model, kill switch, AUP discipline
├── compliance-checklist.md  # if you fork this, run through this list
└── LICENSE               # Apache 2.0
```

## Compliance checklist

If you fork this repo and run an agent against a real venue, please read [`compliance-checklist.md`](./compliance-checklist.md) first. It's short. It covers the things that, if you skip them, make your agent a problem for the venue and a liability for you.

## Contributing

Issues and PRs welcome. We're particularly interested in:

- v0.2 schema-validation feedback against the [Kitso Handshake v0.2](https://kitsuno.ai/handshake/v0.2/) schemas
- Counter-agent implementations that use `kitso-policy-match` and report integration friction
- Reference seeker-agent implementations for venues we haven't tested
- Anything the SECURITY.md threat model misses

What we won't accept: PRs that loosen the fence pattern, PRs that add LLM-in-the-loop verbs without equivalent gating, PRs that remove the AUP and rate-limit defaults.

## A note on propagation

The most useful thing this repo can do is be small, readable, and unsurprising. A working agent in 600 lines beats a featureful agent in 6000. We will optimize for clarity, not feature surface.

## License

Apache License 2.0. See [`LICENSE`](./LICENSE).

---

**Repository:** github.com/kitsuno-ai/kitso-handshake-agents
**Spec:** github.com/kitsuno-ai/kitso-handshake · [kitsuno.ai/handshake/v0.2/](https://kitsuno.ai/handshake/v0.2/)
**Contact:** handshake@kitsuno.ai
**Published by:** Kitsuno · kitsuno.ai
