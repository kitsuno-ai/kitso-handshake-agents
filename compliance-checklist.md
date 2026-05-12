# Compliance Checklist

If you fork this repo and run an agent against a real venue, please read this first. It is short. It covers things that, if you skip them, make your agent a problem for the venue and a liability for you.

## Before you write any code

- [ ] You have read the [Kitso Handshake v0.1 spec](https://github.com/kitsuno-ai/kitso-handshake)
- [ ] You understand the **consent grammar** (which fields are auto-disclosable, confirmation-required, human-only, forbidden)
- [ ] You understand the **trust tiers** and which one your agent will claim
- [ ] You have read the target venue's terms of service and acceptable use policy

## Identity and provenance

- [ ] Your Vacancy Agent Cards declare a `provenance.tier` you can actually defend (don't claim `domain_verified` if you can't verify the domain)
- [ ] Your agent's `User-Agent` string identifies the agent honestly
- [ ] Your agent's principal is a real human or legal entity, named in the operator's records and reachable via the venue's standard contact mechanism
- [ ] If you use `principal_type: rpo`, you can produce written authorization from the underlying hiring entity on request

## Consent

- [ ] You have explicit, time-bound authorization from your principal for the agent's actions
- [ ] The authorization specifies `bindability` correctly (most agents should be `negotiate_and_surface_only`, not `may_bind`)
- [ ] Your `consent_policy.agent_may_invite_without_human_review` is `true` only if you genuinely have a human reviewing every invitation; otherwise mark it `false`
- [ ] Your privacy policy explains the agent's role to the humans it represents and the humans it contacts

## Schema compliance

- [ ] Every vacancy card you publish validates against `https://kitsuno.ai/handshake/v0.1/vacancy-agent-card.json`
- [ ] Every seeker card you publish validates against `https://kitsuno.ai/handshake/v0.1/seeker-agent-card.json`
- [ ] Every invitation you send validates against `https://kitsuno.ai/handshake/v0.1/invitation.json`
- [ ] Every disclosure you send validates against `https://kitsuno.ai/handshake/v0.1/disclosure.json`
- [ ] You run schema validation in CI on every change to a card or template

## Security posture (from SECURITY.md)

- [ ] Your agent enforces the **three-layer fence**: LLM agility inside, deterministic gate at, sealed capabilities outside
- [ ] Your agent has a **kill switch** documented and tested
- [ ] Your agent writes to an **audit DB isolated from production**
- [ ] Your agent **rate limits itself** to the venue's published AUP
- [ ] Your agent **fails closed** when env vars are missing
- [ ] Your agent **redacts credentials in logs**

## Venue good-citizenship

- [ ] You have read the venue's terms of service
- [ ] You are not scaling beyond what the venue intended without explicit venue partnership
- [ ] Your agent **stops on revocation** rather than aggressively re-authenticating
- [ ] You have a **published security contact** for the agent

## Honesty in published artifacts

- [ ] Vacancy cards reflect actual hiring intent (real role, real bar, real timeline)
- [ ] Seeker cards reflect actual principal stance (don't claim `active_search` if the human just left their last role and is decompressing)
- [ ] The principal's `consent_policy` matches what the principal actually agreed to, in writing, with a TTL

## After launch

- [ ] You **monitor** the audit DB for anomalies (sudden volume changes, repeated failures, unusual classifications)
- [ ] You **retro** monthly: what did the agent do, was it what the principal wanted, what should change
- [ ] You **rotate credentials** on a schedule and immediately on suspected exposure
- [ ] You **review** any agent posting that surfaced a complaint from the venue or a counterparty

## If something goes wrong

- [ ] You can hit the kill switch within 60 seconds
- [ ] You can produce the full audit trail for any decision the agent made
- [ ] You can answer "who authorized this agent to act?" with a written, dated, scope-bounded authorization
- [ ] You are prepared to apologize, fix, and explain — not to argue

---

If you can't tick most of these boxes confidently, please don't run an agent against a real venue. Build, test, and tick first.
