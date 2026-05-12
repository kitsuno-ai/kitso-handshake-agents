# Security

This document describes the security posture every agent in this repo follows. It is not optional. If you fork this code and remove or weaken the controls described here, you take on responsibility for the consequences.

## Threat model

Agents in this repo operate against venues where:

- Posts and other content are **untrusted text** from arbitrary parties (including other agents)
- The venue platform itself may have weak security posture (vibe-coded APIs, exposed databases, prior leak history)
- Other agents may attempt **prompt injection** to manipulate behavior
- API keys may be exposed through platform incidents outside the operator's control

The agents are designed to be **safe to leave running** in this environment. This means: even if any single component is compromised (LLM output manipulated, API key leaked, classifier confused), the system cannot escalate beyond its scoped capabilities.

## The fence pattern

Every agent in this repo enforces the same three-layer architecture.

### 1. Inside the fence — LLM agility

The LLM is free to be smart. It classifies, ranks, summarizes, reasons. But:

- Output is **structured** (JSON schema, fixed shape)
- Temperature is **low** (0.0–0.2)
- No **chat-completion**-shaped prompts — the LLM is a classifier, not a conversationalist
- Free-form text (e.g. `reasoning`) is **logged only, never acted on programmatically**
- Untrusted content is passed inside a **fenced data block** with the system prompt explicit that fenced content is data, not instructions

### 2. At the fence — deterministic gate

Every action against the outside world passes through Python code, not the LLM. The LLM proposes; the gate decides:

- **Allowlists** for URLs, domains, submolts, recipients
- **Thresholds** for confidence scores (e.g. `relevance >= 0.7` before any outbound action)
- **Rate limits** with per-venue ceilings
- **Schema validation** on any structured output before acting on it
- **Idempotency keys** to prevent double-execution

If the LLM proposes a URL not on the allowlist, the gate drops the proposal and logs it. The LLM is never trusted to construct an HTTP request or pick a recipient.

### 3. Outside the fence — sealed

Each agent declares its **capabilities** as a fixed list of pre-built Python functions. There are no other verbs available. Specifically:

- **No shell access** (no `subprocess`, no `os.system`)
- **No arbitrary HTTP** (only the venue API client and allowlisted card-host clients)
- **No file writes** outside the audit DB schema
- **No network egress** beyond the allowlisted set
- **No access to the host environment** beyond declared env vars

A prompt-injection attack that successfully manipulates the LLM's output cannot escalate, because the verbs the LLM can "use" don't exist in its environment. The LLM produces a JSON blob; the gate decides what, if anything, to do with it.

## Kill switch

Every agent in this repo MUST be stoppable with a single command. The reference implementation supports:

```bash
curl -X POST $AGENT_HOST/kill \
  -H "X-Kill-Token: $AGENT_KILL_TOKEN"
```

On receipt, the agent:
1. Logs the kill event with timestamp and source IP
2. Completes any in-flight write to the audit DB
3. Exits with code 0

Operators MUST set `AGENT_KILL_TOKEN` to a random value and store it where they can reach it under pressure (e.g. a phone-accessible notes app, not in the same git repo as the deployment).

## Audit trail

Every classification, every outbound action, every error is logged to a separate `audit` database:

- **Schema:** append-only, never updated
- **Retention:** 30 days default, then automatic wipe (operator-configurable)
- **Isolation:** the audit DB MUST NOT share credentials with any production database. If your agent uses a Postgres instance shared with another service, you have skipped a guardrail
- **Erasure:** GDPR / right-to-be-forgotten requests are honored within 7 days. The audit trail must support per-subject deletion

## AUP discipline

Agents in this repo are designed to be good citizens of the venues they operate in. Specifically:

- **Respect rate limits.** The reference Moltbook client enforces 1 post / 30 min / agent (the venue's published limit). Do not raise this in a fork without explicit venue permission.
- **Identify the agent honestly** in `User-Agent` and (where applicable) Agent Card. No claiming verification you don't have. No spoofing other agents' identities.
- **Stop on revocation.** If the venue revokes your API key, stop. Do not try to re-authenticate aggressively. Document the revocation and move on.
- **No mass action.** These agents post low-volume content (single-digit posts per day per agent). Forks that scale beyond that without venue partnership are abuse, not adoption.

## Credential handling

- API keys, tokens, and other secrets MUST be supplied via environment variables, never committed to source. The provided `.env.example` lists the required variables; the agent fails closed if any are missing.
- Secrets MUST NOT appear in logs. The audit DB schema redacts known credential patterns.
- Operators SHOULD rotate credentials on a schedule and immediately on suspected exposure.
- **Assume the venue's API key will leak.** Design so leakage is harmless: the credential should grant only the permissions the agent actually uses, scoped to a single venue, with no read access to anything sensitive.

## Reporting vulnerabilities

If you find a security issue in this code, please email **security@kitsuno.ai** rather than filing a public issue. We will acknowledge within 3 working days and aim to triage within 7.

## Operator's checklist before first run

- [ ] Kill switch tested in dry-run; you have the kill URL and token in two places
- [ ] Audit DB is on a separate database/credential from any production data
- [ ] All required env vars are set; agent fails closed without them
- [ ] Allowlist matches your intended scope (no wildcard egress, no broad URL patterns)
- [ ] Rate limits in config match the venue's published AUP
- [ ] Logs do not contain secrets (test by grepping a sample run)
- [ ] You have read [`compliance-checklist.md`](./compliance-checklist.md)

If any of these are not true, do not deploy.
