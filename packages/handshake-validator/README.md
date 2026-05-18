# handshake-validator

Reference implementation of the **L2 → L3-eligible quality gate** for the [Kitso Handshake](https://kitsuno.ai/handshake/v0.2/) protocol (v0.2.2+).

The validator decides whether an agent-to-agent handshake between a seeker and a vacancy is worth surfacing to a human. It is the protocol's anti-spam mechanism: every strong-fit verdict commits a human's attention; everything else is silently dropped.

## What the validator does

After both sides of a handshake have exchanged L2 disclosures, the validator examines the (seeker_card, vacancy_card) pair and produces one of three verdicts:

| Verdict | Meaning | Effect on the conversation |
|---|---|---|
| `strong_fit` | All four fit dimensions match. Worth a human's attention. | Conversation becomes L3-eligible. Seeker sees the match in their pipeline. |
| `weak_fit` | Adjacent but not core. Mixed dimension signals. | Silent drop. Stored for analytics; no notification. |
| `no_fit` | Structurally a different kind of work despite passing policy filters. | Silent drop. Stored for analytics; no notification. |

Verdicts include four structured `fit_dimensions` for downstream analytics and tuning:

- **`role_alignment`** — does the work itself overlap, beyond role family?
- **`seniority_fit`** — does the seniority expectation match the seeker's range?
- **`skill_overlap`** — substantive overlap, not just keyword surface?
- **`context_fit`** — country, language, employment shape, work mode?

A `low_signal` flag is set automatically when the vacancy's description is shorter than ~800 characters — distinguishing *"weak fit because the data is thin"* from *"weak fit because actually weak"*.

## Why this matters: the anti-spam principle

A pipeline is a commitment surface, not a feed. Every strong fit that lands there says *"this is worth the human's attention."* If the pipeline starts filling with merely-adjacent roles, the human learns to ignore the pipeline — and at that point the whole handshake protocol has nothing to show for itself.

Implementations **must** be conservative about `strong_fit`. A missed strong is recoverable; a noisy pipeline erodes the only thing the protocol was meant to protect.

## Install

```bash
pip install handshake-validator
```

## Use

### As-is — deterministic baseline

```python
from handshake_validator import RuleBasedValidator

validator = RuleBasedValidator()
verdict = validator.validate(seeker_card, vacancy_card)

if verdict.verdict.value == "strong_fit":
    surface_to_seeker_pipeline(verdict)
else:
    # silent drop; verdict still stored for analytics
    persist_for_tuning(verdict)
```

The `RuleBasedValidator` is fully deterministic, no LLM, no network. It scores against the four protocol dimensions using only the structured fields in the v0.2 card schemas. Use it when:

- You want to run the handshake protocol end-to-end without an LLM dependency
- You want a baseline to grade your own classifier against
- You need a no-network fallback when an LLM-backed validator fails

It is not state-of-the-art. It is, by design, the simplest thing that satisfies the contract.

### As a base — plug in your own classifier

```python
from handshake_validator import HandshakeValidator, Verdict, FitVerdict, FitDimension

class MyValidator(HandshakeValidator):
    def validate(self, seeker_card, vacancy_card):
        # your model call here
        ...
        return Verdict(
            verdict=FitVerdict.STRONG,
            reason="Senior platform engineer role aligned with your Python+K8s targets.",
            fit_dimensions={
                "role_alignment": FitDimension.MATCH,
                "seniority_fit": FitDimension.MATCH,
                "skill_overlap": FitDimension.MATCH,
                "context_fit":   FitDimension.MATCH,
            },
            low_signal=self.is_low_signal(vacancy_card),
        )
```

Subclasses **must** return a `Verdict` from `validate()`. Implementations are required to handle their own failure modes — degrade to `weak_fit`, never raise to the caller. The protocol relies on every conversation getting a verdict so its state can move forward.

For an end-to-end LLM-backed example (with placeholders marked `# TUNE THIS`), see [`examples/llm_validator_template.py`](examples/llm_validator_template.py).

## Building a good validator: practical guidance

If you're writing an LLM-backed validator, the protocol leaves the model and rubric to you. Here is what we've learned running ours at production volume:

### Rubric

Your rubric is the heart of validator quality. It should be:

- **Specific about your corpus's failure modes.** Generic "judge fit" prompts hallucinate. Spell out the failure pattern you're trying to catch ("this corpus is full of consulting roles in the same role-family as engineering targets — call those `no_fit`, not `partial`").
- **Explicit about precision vs recall.** The protocol's anti-spam stance means you want high precision on `strong_fit`. Say so to the model.
- **Forbidden from inventing context.** Models extrapolate. Tell yours: "if a field is missing, treat it as unknown — never infer mismatch from absence."
- **Monolingual English.** The protocol's canonical card fields are English-language slugs and ISO codes. Localisation happens upstream of the validator.

### Model selection

- **Temperature 0** for classifications. You want determinism, not creativity.
- **Small or mid-size is usually enough.** This is a four-dimension classification with structured inputs, not summarisation. We see clean signal from models at the 8B–32B class.
- **Cascade providers.** Free-tier rate limits will bite you. A two- or three-provider cascade (free → free → small-paid) keeps the validator alive without dominating cost.

### Output handling

- **Tolerant JSON parsing.** Even at temperature 0, models prepend ```json fences, apology preambles, and trailing commentary. Strip these before parsing.
- **Sanitise the `reason` field.** It ends up in a candidate-facing UI. Treat model output as input-tainted: strip HTML, collapse whitespace, cap length.
- **Always produce a verdict.** If the model 4xx's, times out, or returns garbage twice in a row, fall back to `weak_fit` with `low_signal=true`. Never raise.

### Cost control

- **Cache by input state.** The protocol's `state_hash` ([kitso-state-hash](../kitso-state-hash/)) gives you a stable hash of the (seeker, vacancy) pair's matching-relevant fields. Cache verdicts keyed by `(seeker_hash, vacancy_hash)`; skip the LLM call when nothing has changed.
- **Fire once per conversation, not once per scan.** The validator is a state transition (L2 → L3-eligible), not a scoring pass. Wire it into the state machine, not into a recurrent crawler.

## Verdict contract

```python
@dataclass(frozen=True)
class Verdict:
    verdict: FitVerdict                          # strong_fit | weak_fit | no_fit
    reason: str                                  # one sentence, <=200 chars, sanitised
    fit_dimensions: Mapping[str, FitDimension]   # role/seniority/skill/context -> match/partial/miss
    low_signal: bool                             # vacancy description thinner than threshold
    extras: Mapping[str, Any]                    # implementation-defined; protocol doesn't constrain
```

`verdict.to_dict()` produces a JSON-compatible payload matching the spec's §validator output shape — safe to persist on a `handshake_conversations` row or send across an A2A boundary.

## Spec

The normative spec lives in [`kitso-handshake`](https://github.com/kitsuno-ai/kitso-handshake) under `schemas/v0.2/index.html` (the `#validator` section). This package implements §validator of that spec.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

Tests use the canonical v0.2 fixtures at `../../test-fixtures/v0.2/` — any future schema clean-up of those fixtures must keep these tests green.

## License

Apache-2.0. The validator interface and the rule-based reference are open. Operators are encouraged to fork, replace the classifier, and share back findings.
