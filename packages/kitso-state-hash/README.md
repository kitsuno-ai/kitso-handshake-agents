# kitso-state-hash

Reference Python implementation of the **state_hash** primitive for the Kitso Handshake protocol (v0.2.1+).

The `state_hash` lets any two agents agree on whether they have already evaluated a (seeker, vacancy) pair *at the same observable state*. When either side mutates, its hash changes, and prior evaluations become stale — naturally invalidating cached idempotency.

## What it does

```python
from kitso_state_hash import vacancy_state_hash, seeker_state_hash

# 64-char lowercase hex SHA-256
vac_h = vacancy_state_hash(vacancy_card_dict)
see_h = seeker_state_hash(seeker_card_dict)
```

## How it works

```
state_hash = lowercase_hex( SHA-256( JCS(canonical_subset(card)) ) )
```

- **`canonical_subset`** is the spec-defined subset of card fields that affect matching. Cosmetic edits (slug, description, company_name) don't change the hash; semantic changes (skills, country_codes, salary_min) do.
- **JCS** is JSON Canonicalization Scheme ([RFC 8785](https://datatracker.ietf.org/doc/html/rfc8785)): sorted keys, no insignificant whitespace, shortest round-trip number form.
- **SHA-256** as defined in [FIPS 180-4](https://csrc.nist.gov/publications/detail/fips/180/4/final).

## Spec

The normative spec lives in the [kitso-handshake](https://github.com/kitsuno-ai/kitso-handshake) repo. This package implements §2 of that spec.

## Compatibility

- Python ≥ 3.9
- Zero runtime dependencies (stdlib `hashlib` + a minimal JCS impl in `jcs.py`)
- Apache-2.0 licensed

## Verification

Test vectors live at `../../test-fixtures/v0.2/state-hash/`. Any implementation in any language **must** produce byte-identical hashes for those fixtures.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```
