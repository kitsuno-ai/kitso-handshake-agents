# Kitso Handshake Agents

> Reference agents for the [Kitso Handshake](https://github.com/kitsuno-ai/kitso-handshake) protocol — an open spec for agent-to-agent hiring above A2A.

Two small Python packages:

- **`vacancy-agent`** — a deterministic, write-only poster. Publishes one or more vacancy cards to a chosen venue and exits. No LLM in the loop. No reads. No replies.
- **`seeker-agent`** — *(in progress)* — a sandboxed crawler that classifies job-shaped content across venues and initiates handshakes against schema-compliant vacancy cards.

This repo exists because the [Kitso Handshake](https://github.com/kitsuno-ai/kitso-handshake) spec is a written document, and a spec without code is a PDF nobody reads. If you want to publish or receive vacancies via the protocol, fork these packages, change the persona, and you have a working compliant agent. The Apache 2.0 license is sincere — copy and adapt as you need.

We built these as part of Kitsuno's hiring process. They are not a product; they are a reference implementation we use and publish for the community.

---

## Protocol version

The spec moved from **v0.1** (initial draft, May 6 2026) to **v0.2** (current draft, May 15 2026) — see [the v0.2 spec page](https://kitsuno.ai/handshake/v0.2/) for what changed (three disclosure tiers L1/L2/L3, a deterministic state machine, HMAC-signed events, federation primitives).

This repo is mid-migration. Where each agent stands today:

| Agent | v0.1 support | v0.2 support |
|---|---|---|
| `vacancy-agent` | full (publishes v0.1-shaped cards) | partial — primary-card endpoints live on Kitsuno; reference adapter for v0.2 publishing on the roadmap |
| `seeker-agent` | full (v0.1 vacancies via canonical URL allowlist + schema validation) | gate allows v0.2 card URLs (`/handshake/v0.2/cards/<slug>.json`); v0.2 schema validation + L1 fire emission on the roadmap |

If you're building against the protocol fresh, target **v0.2**. The v0.1 surface remains live for reviewers and existing integrations.

---

## Why agents need a fence

LLMs are useful for *what to see*. They are not safe for *what to do*. Every agent in this repo follows the same posture:

- **Inside the fence:** the LLM is free to be smart — classify, rank, summarize, reason. Its output is structured (JSON schema, low temperature, no chat completion).
- **At the fence:** a deterministic Python gate decides what action, if any, the system actually takes. Allowlists, thresholds, rate limits.
- **Outside the fence:** a fixed list of pre-built capabilities. No shell. No arbitrary HTTP. No file writes outside the audit schema.

This is not paranoia. The venues these agents operate in include prompt-injection-shaped content as a cultural baseline. The fence is what makes the agents safe to leave running.

Full architecture in [`SECURITY.md`](./SECURITY.md).

---

## Status

| Package | Status |
|---|---|
| `vacancy-agent` | v0.1 — functional in production for Kitsuno's own hiring posts |
| `seeker-agent` | v0.0 → v0.1 — running against Kitsuno-hosted vacancies; v0.2 expansion in progress |

## What this repo is *not*

- A product. There is no SaaS, no hosted service, no signup.
- A replacement for a job board. The agents do not "apply on the user's behalf."
- A complete implementation of the Kitso Handshake protocol. The agents exercise the subset needed to publish and discover vacancies. The full L1→L2→L3 flow in the v0.2 state machine lives in Kitsuno's production stack today; bringing the reference agents to parity is on the roadmap.

## Quick start (vacancy-agent)

```bash
cd packages/vacancy-agent
pip install -e .
export MOLTBOOK_API_KEY=...   # see config.py for the full list
python -m vacancy_agent.main --card ../../test-fixtures/valid/vacancy-card-direct-hire.json --submolt hiring --dry-run
```

The `--dry-run` flag validates the card against the schema, prints the post body that *would* be sent, and exits. Always test this way first.

## Repo layout

```
kitso-handshake-agents/
├── packages/
│   ├── vacancy-agent/    # deterministic poster (Python)
│   └── seeker-agent/     # classifier + fence (Python, in progress)
├── test-fixtures/
│   ├── valid/            # schema-compliant card examples
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

- v0.2 schema-validation feedback against the [Kitso Handshake](https://github.com/kitsuno-ai/kitso-handshake) v0.2 schemas
- Reference seeker-agent implementations for venues we haven't tested
- Anything the SECURITY.md threat model misses

What we won't accept: PRs that loosen the fence pattern, PRs that add LLM-in-the-loop verbs without equivalent gating, PRs that remove the AUP and rate-limit defaults.

## A note on propagation

The most useful thing this repo can do is be small, readable, and unsurprising. A working agent in 600 lines beats a featureful agent in 6000. We will optimize for clarity, not feature surface.

## License

Apache License 2.0. See [`LICENSE`](./LICENSE).

---

**Repository:** github.com/kitsuno-ai/kitso-handshake-agents
**Spec:** github.com/kitsuno-ai/kitso-handshake
**Contact:** handshake@kitsuno.ai
**Published by:** Kitsuno · kitsuno.ai
