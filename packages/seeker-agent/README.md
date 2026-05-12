# seeker-agent

**Status: v0.0 — sketch.** Not yet implemented.

The Seeker Agent will be the read-side counterpart to `vacancy-agent`. Where Vacancy posts the contract, Seeker discovers it: polls Moltbook (and, in v2, fetch.ai + A2A discovery nodes), classifies posts via a sandboxed LLM, and initiates handshakes against schema-compliant vacancy cards.

The design follows the **three-layer fence pattern** described in [`../../SECURITY.md`](../../SECURITY.md):

- **Inside the fence:** structured-output LLM classifier (JSON schema, low temperature). Output fields: `is_job_shaped`, `relevance`, `extracted_role`, `extracted_company`, `venue`, `reasoning` (logged only).
- **At the fence:** Python gate. `relevance >= 0.7` → consider for handshake. URL matches allowlist → consider for `read_vacancy_card`. Rate limits enforced.
- **Outside the fence:** five named verbs only — `fetch_next_moltbook_page`, `read_vacancy_card`, `classify_post`, `log_classification`, `initiate_handshake`, `post_field_note` (≤1/day, second-LLM-validated).

A parallel arm runs the same classifier against the existing `gonzo_*` market_data feed (read-only) for apples-to-apples scoring across the two tiers (pure-agent vs human-with-bots).

Implementation lands in S293+. The spec is at [`../../docs/architecture.md`](../../docs/architecture.md) and the field study it serves is at [`../../docs/experiment-s291.md`](../../docs/experiment-s291.md).
