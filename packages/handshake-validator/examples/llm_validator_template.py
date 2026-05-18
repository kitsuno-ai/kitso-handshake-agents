"""Example: an LLM-backed validator that satisfies the HandshakeValidator contract.

This file is a *teaching scaffold*, not a production classifier. Run it as-is
and you'll get a working validator that calls an LLM and produces verdicts;
the verdict quality will be okay-ish — fine for a baseline, not what an
operator running real handshakes at volume should ship.

What you must change before this is useful to you:

    # TUNE THIS — the rubric is the heart of validator quality.
    # The version below is intentionally generic. A good rubric:
    #   - Is specific about your corpus's failure modes
    #   - Tells the model what to refuse (hallucinated context, ungrounded inference)
    #   - Sets the precision/recall preference explicitly
    #   - Is written in monolingual English (the protocol runs on
    #     canonicalised English fields; language localisation happens
    #     upstream of the validator).

    # TUNE THIS — the model + provider cascade is your cost control.
    # Single-provider is fragile under free-tier rate limits. A cascade
    # (e.g. small free model -> mid free model -> tiny paid model) keeps
    # the validator alive without burning budget. The order matters.

    # TUNE THIS — the JSON parser must tolerate the chatter your model adds.
    # Even at temperature 0, models prepend "```json", apology preambles,
    # and trailing commentary. Production parsers are very forgiving.

Nothing in this file is a Kitsuno operational secret. The rubric is generic.
The model name is a placeholder. The cascade is one provider. Real operators
should replace all three.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

from handshake_validator import (
    FitDimension,
    FitVerdict,
    HandshakeValidator,
    Verdict,
)


# ----------------------------------------------------------------------
# TUNE THIS — generic rubric. Replace with one tuned to your corpus.
# ----------------------------------------------------------------------

GENERIC_SYSTEM_PROMPT = """You are a fit validator for an agent-to-agent hiring protocol.

You will receive a job seeker's structured profile and a vacancy's structured
posting. Both sides have already passed structural compatibility filters
(country, language ability, role family, basic seniority overlap). Your job
is the semantic fit pass.

Read both inputs. Decide whether the match is worth a human's attention.

Output strict JSON in this shape, and nothing else:

{
  "verdict": "strong_fit" | "weak_fit" | "no_fit",
  "reason": "<one sentence, under 200 characters>",
  "fit_dimensions": {
    "role_alignment":   "match" | "partial" | "miss",
    "seniority_fit":    "match" | "partial" | "miss",
    "skill_overlap":    "match" | "partial" | "miss",
    "context_fit":      "match" | "partial" | "miss"
  }
}

STRONG FIT requires all four dimensions to genuinely match — beyond surface
keyword overlap. If you are uncertain, return weak_fit. A missed strong is
recoverable; a noisy strong erodes trust in the pipeline.

Do not invent details that are not in the inputs. If a field is missing,
treat it as unknown, not as a reason to call mismatch.
"""


class LLMValidatorTemplate(HandshakeValidator):
    """Template showing how to wire an LLM behind the HandshakeValidator interface.

    Plug in a ``llm_call`` callable that takes ``(system_prompt, user_prompt)``
    and returns the raw model response string. The class handles input
    bundling, prompt construction, JSON parsing, and the fallback-to-weak
    guarantee that the protocol requires.
    """

    def __init__(
        self,
        llm_call: Callable[[str, str], str],
        system_prompt: str = GENERIC_SYSTEM_PROMPT,
    ) -> None:
        self._llm_call = llm_call
        self._system_prompt = system_prompt

    def validate(
        self,
        seeker_card: Mapping[str, Any],
        vacancy_card: Mapping[str, Any],
    ) -> Verdict:
        user_prompt = self._build_user_prompt(seeker_card, vacancy_card)
        low_signal = self.is_low_signal(vacancy_card)

        try:
            raw = self._llm_call(self._system_prompt, user_prompt)
        except Exception:
            # The protocol REQUIRES we produce a verdict. Failures degrade to weak.
            return self._fallback_weak(low_signal=low_signal, why="model call failed")

        parsed = _parse_verdict_json(raw)
        if not parsed:
            return self._fallback_weak(low_signal=low_signal, why="model output unparseable")

        try:
            return Verdict(
                verdict=FitVerdict(parsed.get("verdict", "weak_fit")),
                reason=parsed.get("reason", ""),
                fit_dimensions={
                    k: FitDimension(v)
                    for k, v in (parsed.get("fit_dimensions") or {}).items()
                    if k in {"role_alignment", "seniority_fit", "skill_overlap", "context_fit"}
                },
                low_signal=low_signal,
                extras={"classifier": "llm_template"},
            )
        except (ValueError, KeyError):
            return self._fallback_weak(low_signal=low_signal, why="model output invalid")

    # ------------------------------------------------------------------

    def _build_user_prompt(
        self,
        seeker: Mapping[str, Any],
        vacancy: Mapping[str, Any],
    ) -> str:
        # TUNE THIS — input bundling drives token cost and signal quality.
        # The protocol gives you both L1 and L2 fields by the time the
        # validator fires. Include what's signal-bearing; drop what isn't.
        # For a real operator, you almost certainly want to trim seeker
        # narrative fields, anonymise employer names, and cap evidence lists.
        seeker_view = {
            "role_targets": seeker.get("role_targets"),
            "skills": seeker.get("skills"),
            "languages": seeker.get("languages"),
            "geography": seeker.get("geography"),
            "employment_types": seeker.get("employment_types"),
        }
        vacancy_view = {
            "title": vacancy.get("title"),
            "role_family": vacancy.get("role_family"),
            "seniority_level": vacancy.get("seniority_level"),
            "skills": vacancy.get("skills"),
            "nice_to_have_skills": vacancy.get("nice_to_have_skills"),
            "languages_required": vacancy.get("languages_required"),
            "country_codes": vacancy.get("country_codes"),
            "employment_type": vacancy.get("employment_type"),
            "description": vacancy.get("description"),
        }
        return (
            "SEEKER:\n"
            + json.dumps(seeker_view, ensure_ascii=False, indent=2)
            + "\n\nVACANCY:\n"
            + json.dumps(vacancy_view, ensure_ascii=False, indent=2)
        )

    def _fallback_weak(self, low_signal: bool, why: str) -> Verdict:
        return Verdict(
            verdict=FitVerdict.WEAK,
            reason=f"Validator fallback ({why}); classified conservatively.",
            fit_dimensions={
                "role_alignment": FitDimension.PARTIAL,
                "seniority_fit": FitDimension.PARTIAL,
                "skill_overlap": FitDimension.PARTIAL,
                "context_fit": FitDimension.PARTIAL,
            },
            low_signal=low_signal,
            extras={"classifier": "llm_template", "fallback": True, "why": why},
        )


# ----------------------------------------------------------------------
# Tolerant JSON parser — production parsers are *more* forgiving than this.
# ----------------------------------------------------------------------


def _parse_verdict_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    text = raw.strip()
    # Strip ```json fences if the model added them.
    if text.startswith("```"):
        text = text.split("```", 2)[-1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        text = text.rsplit("```", 1)[0]
        text = text.strip()
    # If the model added a preamble before the JSON, find the first '{'.
    if not text.startswith("{"):
        idx = text.find("{")
        if idx == -1:
            return None
        text = text[idx:]
    # If it added a postamble after the JSON, cut at the last '}'.
    if not text.endswith("}"):
        idx = text.rfind("}")
        if idx == -1:
            return None
        text = text[: idx + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ----------------------------------------------------------------------
# Demo entry-point — run with `python llm_validator_template.py`
# ----------------------------------------------------------------------


if __name__ == "__main__":
    # Sample llm_call that ALWAYS returns weak_fit, just so the file is runnable
    # without network. Replace with a real call to whichever provider you use.
    def _stub_call(system: str, user: str) -> str:
        return json.dumps(
            {
                "verdict": "weak_fit",
                "reason": "Stub LLM: replace _stub_call with a real provider call.",
                "fit_dimensions": {
                    "role_alignment": "partial",
                    "seniority_fit": "partial",
                    "skill_overlap": "partial",
                    "context_fit": "partial",
                },
            }
        )

    validator = LLMValidatorTemplate(llm_call=_stub_call)
    sample_seeker = {
        "role_targets": [
            {
                "role_family": "software_engineering",
                "role_title_keywords": ["python engineer"],
                "seniority_range": ["senior", "lead"],
            }
        ],
        "skills": ["python", "kubernetes"],
        "languages": [{"language": "en", "level": "C1"}],
        "geography": {"countries": ["CH"], "scope": "regions"},
        "employment_types": ["full_time"],
    }
    sample_vacancy = {
        "title": "Senior Python Engineer",
        "role_family": "software_engineering",
        "seniority_level": "senior",
        "skills": ["python", "postgres"],
        "country_codes": ["CH"],
        "employment_type": "full_time",
        "description": "Build things.",
    }
    verdict = validator.validate(sample_seeker, sample_vacancy)
    print(json.dumps(verdict.to_dict(), indent=2))
