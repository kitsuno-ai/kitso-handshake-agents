# Changelog

All notable changes to this repo are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/), versioning follows [Semantic Versioning](https://semver.org/).

## [v0.2.2] — 2026-05-19

### New package: `handshake-validator` v0.1.0
- Reference implementation of the v0.2 handshake validator — the L2 →
  L3-eligible quality gate that decides which handshakes are worth
  surfacing to a human.
- API:
  - `HandshakeValidator` abstract base class — operators subclass and plug in
    their own classifier.
  - `RuleBasedValidator` — deterministic, no-LLM, no-network reference. Scores
    against the four protocol dimensions (role_alignment, seniority_fit,
    skill_overlap, context_fit) using only the structured fields in the
    v0.2 card schemas.
  - `Verdict` frozen dataclass — three-bucket verdict (strong_fit / weak_fit
    / no_fit), one-sentence sanitised reason, structured per-dimension grades,
    `low_signal` flag for thin-data vacancies (description < ~800 chars).
- `examples/llm_validator_template.py` — teaching scaffold showing the shape
  of an LLM-backed validator with placeholders marked `# TUNE THIS` for
  rubric, model selection, and JSON parsing. Generic, not Kitsuno's recipe.
- 12 fixture-driven tests covering: aligned-pair STRONG, family-only PARTIAL,
  family-miss NO_FIT, seniority distance grading, low-signal flagging,
  country exclusion / global scope, verdict serialisation, reason
  sanitisation.
- Spec: §validator of `kitso-handshake` v0.2.2 (additive; existing
  conversations without validator metadata still parse).
- Apache-2.0. Zero runtime dependencies.

## [v0.2.1] — 2026-05-19

### Changed
- **`work_permit` field renamed to `work_rights` across the protocol** to match
  production reality. Three deployed seeker cards already use `work_rights`;
  zero use `work_permit`. The S337 rename (introduced when no real cards
  existed yet) is reverted. Updates touch: `packages/policy-match` alias map,
  region-aware fields, docstrings, examples, tests, and the v0.2
  `seeker-card-engineering` + `handshake-policy-direct-hire` fixtures.
- **`policy-match` 0.2.0 → 0.2.1**: API unchanged for callers that already use
  `work_rights`. Callers passing `work_permit` as a criterion field name
  continue to resolve correctly (alias preserved).

### Added
- **`min_matches` parameter on the `any` operator**: criteria can now require
  N-of-the-listed-values overlap (e.g. `{"operator": "any", "values": [...],
  "min_matches": 2}`). Defaults to 1 for backward compatibility. Block reasons
  now report `len(overlap) of required min_matches` for diagnosability.
- **Split-shape trait unwrapping for `languages`**: when a seeker stores
  languages as `{speaks: [...], works_in: [...]}` (the work-language gate
  introduced for distinguishing conversational fluency from professional
  working language), `policy_match` evaluates criteria against the inner
  `works_in` list. Legacy flat list / `[{language, level}, ...]` shapes
  continue to work via pass-through.
- **6 new tests** in `tests/test_policy_match.py` covering both additions.

### New package: `kitso-state-hash` v0.1.0
- Reference Python implementation of the v0.2 `state_hash` primitive
  (pair-state idempotency).
- API: `vacancy_state_hash(card)`, `seeker_state_hash(card)`,
  `canonical_subset_vacancy(card)`, `canonical_subset_seeker(card)`.
- Algorithm: `lowercase_hex(SHA-256(JCS(canonical_subset(card))))`. JCS per
  RFC 8785, SHA-256 per FIPS 180-4. Zero runtime dependencies.
- Spec: §2 of the Kitso Handshake state-hash spec (Pair-State Idempotency).
- Tests: 26 pass — round-trip determinism, sort stability, missing-optional
  invariance, §2.4.1 split-shape language flattening, CEFR-aware dedup,
  unicode pass-through, JCS number normalization, JCS object-key sorting.
- Golden test vectors: 14 fixtures at `test-fixtures/v0.2/state-hash/` with
  equivalence groups for cross-language verification.

### Note on the rename
There is no external adopter of the protocol yet; this is the right window to
align names to production. v0.3 will not retain the `work_permit` alias.

## [Unreleased]

### Added
- Repo scaffold: README, LICENSE (Apache 2.0), SECURITY.md, compliance-checklist.md
- `packages/vacancy-agent` — deterministic write-only poster (v0.1, awaiting venue keys for first run)
- `test-fixtures/` — valid and invalid Kitso Handshake v0.1 card examples

### Pending
- `packages/seeker-agent` — sandboxed classifier (in design)
- A2A Invitation/Disclosure flow support
- GitHub Actions CI: schema validation on PRs
