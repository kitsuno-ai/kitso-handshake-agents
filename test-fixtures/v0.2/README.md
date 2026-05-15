# v0.2 test fixtures

Schema-compliant example payloads for the [Kitso Handshake v0.2](https://kitsuno.ai/handshake/v0.2/) protocol. Validated against the published v0.2 schemas (Draft 2020-12).

## Cards (public L1 surface)

| File | Schema | Description |
|---|---|---|
| `vacancy-card-direct-hire-v0.2.json` | [vacancy-card.json](https://kitsuno.ai/handshake/v0.2/vacancy-card.json) | Direct-hire SaaS engineering role, named posting, salary disclosed at L2 (per EU pay-transparency directive default but with `tier_overrides`). |
| `vacancy-card-confidential-v0.2.json` | [vacancy-card.json](https://kitsuno.ai/handshake/v0.2/vacancy-card.json) | Confidential search — `posting_visibility: confidential`, `tier_overrides` move `company_name` to L3, verification tier `challenge_response_verified`. |
| `seeker-card-engineering-v0.2.json` | [seeker-card.json](https://kitsuno.ai/handshake/v0.2/seeker-card.json) | Senior Python/platform engineer, EU work permit, `consent_policy` with explicit per-stage flags and 90-day `scope_expires_at`. |

## Companion files (internal — not part of the published L1 surface)

| File | Description |
|---|---|
| `handshake-policy-direct-hire-v0.2.json` | The gating policy stored alongside the direct-hire vacancy card. **NOT** part of the published card disclosure. Consumed by [`kitso-policy-match`](../../packages/policy-match) at L1/L2 stages. Counter-agents on the seeker side can construct equivalent criteria from the published card's traits. |

## Validating

```python
import json, urllib.request
from jsonschema import Draft202012Validator, RefResolver

vacancy = json.loads(urllib.request.urlopen(
    "https://kitsuno.ai/handshake/v0.2/vacancy-card.json").read())
common  = json.loads(urllib.request.urlopen(
    "https://kitsuno.ai/handshake/v0.2/common.json").read())

resolver = RefResolver.from_schema(vacancy, store={
    common["$id"]: common, vacancy["$id"]: vacancy,
})
validator = Draft202012Validator(vacancy, resolver=resolver)

card = json.load(open("vacancy-card-direct-hire-v0.2.json"))
errors = list(validator.iter_errors(card))
assert not errors, errors
```

## v0.1 fixtures

The previous v0.1 fixtures (`vacancy-card-direct-hire.json`, `vacancy-card-rpo.json`) remain in `../valid/` for reviewers and existing integrations.
