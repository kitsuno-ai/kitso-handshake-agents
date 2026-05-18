# Changelog

All notable changes to this repo are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/), versioning follows [Semantic Versioning](https://semver.org/).

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
