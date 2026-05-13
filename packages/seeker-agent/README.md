# seeker-agent

**Status:** v0.1 scaffold (S295). Gate logic + classifier interface + audit + orchestrator skeleton implemented and tested. Venue clients, DB layer, and real LLM providers ship in S296.

The Seeker Agent is the read-side counterpart to `vacancy-agent`. It polls candidate venues, classifies each visible post against a structured-output schema, logs every classification, and (under explicit conditions) initiates handshakes against schema-compliant vacancy cards.

It runs two arms in parallel against the same classifier:

- **Pure-agent arm:** Moltbook submolt(s), polled directly
- **Human-with-bots arm:** the existing `gonzo_*` channels (HN Who's Hiring, BlueSky, Telegram, Reddit, Lobsters, Mastodon) read from `sf4l_prod.market_data` (read-only)

Output is comparable data across both tiers — same classifier, same schema, same DB — the empirical spine of the field study.

## Architecture — the fence pattern

Three rings:

- **Inside the fence:** LLM classifier. Post text wrapped in `<UNTRUSTED_CONTENT>` tags. Structured-output JSON, schema-validated on receipt. Free-form text only in `reasoning`, which is logged but **never branched on**.
- **At the fence:** Deterministic Python gate. Every proposal from the LLM passes through five checks (relevance threshold, URL allowlist, schema validity, dedup, rate limit). Drops are recorded and not fed back to the LLM (no loop).
- **Outside the fence:** Six named verbs (plus an optional seventh, disabled in v1):
  1. `fetch_next_moltbook_page`
  2. `fetch_next_gonzo_batch`
  3. `classify_post`
  4. `read_vacancy_card`
  5. `log_classification`
  6. `initiate_handshake`
  7. `post_field_note` *(opt-in only — `FIELD_NOTE_ENABLED=false` by default)*

A prompt-injection attack that successfully manipulates the LLM's JSON output cannot escalate — the verbs the LLM can "use" don't exist in its environment. The LLM proposes; the gate decides; the Python code acts.

## Status of each component (S295)

| Component | State | Notes |
|---|---|---|
| `config.py` | ✅ real | Settings for both arms, both providers, allowlists, kill switch |
| `gate.py` | ✅ real, fully tested | Pure functions, no IO |
| `classifier.py` | ✅ interface + EchoProvider | Mistral / Cloudflare integration is S296 |
| `audit.py` | ✅ real | Stdout fallback when no DB configured |
| `verbs.py` | 🟡 stubs | Venue clients + DB land in S296 |
| `main.py` | 🟡 dry-run orchestrator | Lock + loop scaffold; live mode wired in S296 |
| `prompts/classifier-v0.1.md` | ✅ draft | Per design doc §10 |
| `experiment_db` migrations | ⏳ S296 | Schema in design doc §9.1 |
| Cron + kill-switch endpoint | ⏳ S296 | Compose service in `/opt/sf4l-agents/` |

## Configuration

All settings via env (see `config.py` for the full list). Key ones:

| Env var | Default | Purpose |
|---|---|---|
| `SEEKER_LLM_PROVIDER` | `mistral` | `mistral` or `cloudflare` — both free tier |
| `MISTRAL_API_KEY` | *(unset)* | Required for Mistral |
| `CLOUDFLARE_API_TOKEN` | *(unset)* | Required for Cloudflare Workers AI |
| `CLOUDFLARE_ACCOUNT_ID` | *(unset)* | Required for Cloudflare Workers AI |
| `MOLTBOOK_ALLOWED_SUBMOLTS` | *(unset)* | Comma-separated submolt names |
| `SEEKER_RELEVANCE_THRESHOLD` | `0.7` | Below this, all further action is gated |
| `FIELD_NOTE_ENABLED` | `false` | v1 ships disabled |
| `EXPERIMENT_DB_URL` | *(unset)* | Isolated Postgres for classifications + actions |
| `SF4L_PROD_READONLY_URL` | *(unset)* | RO connection to sf4l_prod for the gonzo arm |
| `SEEKER_KILL_TOKEN` | *(unset)* | Random 32-char token for `/kill` endpoint |
| `SEEKER_KILL_FILE` | `/tmp/seeker.kill` | Presence of this file halts the orchestrator |

## Development

```bash
cd packages/seeker-agent
pip install -e .[dev] --break-system-packages
pytest -q
```

## Design doc

The full design (and the resolved §14 open questions) lives at `/opt/sf4l-staging/docs/seeker-agent-design.md` for now. **TODO: move into this repo's `docs/` before public GitHub push** — tracked as an open item.

## License

Apache 2.0.
