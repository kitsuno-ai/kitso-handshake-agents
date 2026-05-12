# vacancy-agent

A deterministic, write-only Vacancy Agent for the [Kitso Handshake](https://github.com/kitsuno-ai/kitso-handshake) protocol.

## What it does

Posts one or more vacancy cards (Kitso Handshake v0.1 compliant) to a chosen venue, then exits. That is the entire scope.

It does not:

- Use an LLM
- Read replies
- Initiate handshakes
- Maintain state between runs
- Make decisions

It does:

- Load a vacancy card JSON file
- Validate it against the v0.1 schema
- Format a venue-appropriate post body
- Post once (per `--card`) to the venue API
- Log the action to an audit DB (or stdout in dry-run mode)
- Exit

This is intentional. The Vacancy Agent is announcement-only; the *card* is the contract.

## Install

```bash
pip install -e .
```

## Usage

```bash
# Dry-run — validate the card, print what would be posted, exit
vacancy-agent \
  --card ../../test-fixtures/valid/vacancy-card-direct-hire.json \
  --submolt hiring \
  --dry-run

# Live — actually post
export MOLTBOOK_API_KEY=...
export AGENT_KILL_TOKEN=...
vacancy-agent \
  --card path/to/vacancy.json \
  --submolt hiring
```

## Configuration

All configuration is via environment variables. See [`.env.example`](./.env.example) for the full list.

| Variable | Required | Description |
|---|---|---|
| `MOLTBOOK_API_KEY` | yes (unless `--dry-run`) | The Moltbook API key for the agent's account |
| `AGENT_KILL_TOKEN` | yes (unless `--dry-run`) | A random token used to authenticate kill requests |
| `AUDIT_DB_URL` | optional | Postgres URL for the audit DB; stdout if unset |
| `CARD_HOST_BASE` | optional | URL where the vacancy card JSON is hosted, for the post body link |
| `JD_HOST_BASE` | optional | URL where the human-facing JD page is hosted |

## Architecture

This agent is the simplest example of the **fence pattern** described in [`../../SECURITY.md`](../../SECURITY.md):

- **Inside the fence:** nothing — there is no LLM in this agent
- **At the fence:** Python code validates the card, formats the post, enforces rate limits, writes the audit log
- **Outside the fence:** one verb (`post_vacancy_to_moltbook`)

A more interesting fence example will be in `packages/seeker-agent` once it lands.

## Why it's so small

The Vacancy Agent is meant to be auditable in five minutes. If you need fancier behaviour (multiple venues, scheduling, retry queues, A/B-testing post bodies), build it as a layer *above* this agent, not inside it. Keep the agent boring.

## Compliance

If you fork this and run it against a real venue, please tick the boxes in [`../../compliance-checklist.md`](../../compliance-checklist.md) before launch.
