"""RuleBasedValidator — deterministic, no-LLM reference implementation.

This validator classifies (seeker, vacancy) pairs using only the structured
fields in the v0.2 card schemas. No model, no network, no API keys. Every
decision is inspectable, every threshold is documented inline.

It is **intentionally a baseline**. A well-tuned LLM validator with access
to vacancy.description and seeker.experience will outperform it on
ambiguous cases. The protocol does not require either — it requires that
*some* validator runs and produces a verdict. This is the simplest one
that satisfies the contract.

Use this when:
  - You want to run the protocol end-to-end without an LLM dependency,
  - You want a baseline to grade your own classifier against,
  - You want a "rule floor" fallback when an LLM-backed validator fails.

Anti-spam principle
-------------------
The validator must be conservative about STRONG. A STRONG verdict will land
on the seeker's pipeline — their commitment surface. A noisy pipeline trains
the seeker to ignore it; a quieter pipeline of higher-precision matches is
what makes Handshake worth opening at all. Better a missed STRONG than a
shaky one.

Threshold rationale (visible, tunable)
--------------------------------------
The thresholds below are not magic. They are calibrated to match the rough
shape of LLM-validator outcomes on the Kitsuno corpus (zero strong fits out
of 66 mirror cards tested in May 2026 was a *correct* signal that the mirror
corpus was dominated by adjacent-but-not-target roles). Operators with
different corpora should tune.

  STRONG  — all four dimensions MATCH
  WEAK    — any MISS, or fewer than two MATCH dimensions
  NO_FIT  — role_alignment is MISS (the strongest single signal)
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from .validator import HandshakeValidator
from .verdict import FitDimension, FitVerdict, Verdict


# Seniority ladder — left-to-right increasing. Used to compute "range overlap".
# Any token not in the ladder is treated as "unknown" and skipped.
SENIORITY_LADDER = (
    "intern",
    "junior",
    "mid",
    "senior",
    "lead",
    "principal",
    "staff",
    "director",
    "vp",
)


class RuleBasedValidator(HandshakeValidator):
    """Deterministic validator scoring four dimensions from card data only."""

    #: Minimum Jaccard overlap on skills to count as ``MATCH`` on skill_overlap.
    #: Below this counts as ``PARTIAL`` if any overlap exists, else ``MISS``.
    skill_match_threshold: float = 0.4

    #: Minimum Jaccard overlap on skills to count as ``PARTIAL``. Below this
    #: is ``MISS``.
    skill_partial_threshold: float = 0.15

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def validate(
        self,
        seeker_card: Mapping[str, Any],
        vacancy_card: Mapping[str, Any],
    ) -> Verdict:
        dimensions = {
            "role_alignment": self._score_role_alignment(seeker_card, vacancy_card),
            "seniority_fit": self._score_seniority_fit(seeker_card, vacancy_card),
            "skill_overlap": self._score_skill_overlap(seeker_card, vacancy_card),
            "context_fit": self._score_context_fit(seeker_card, vacancy_card),
        }
        verdict, reason = self._aggregate(dimensions, vacancy_card)
        return Verdict(
            verdict=verdict,
            reason=reason,
            fit_dimensions=dimensions,
            low_signal=self.is_low_signal(vacancy_card),
            extras={
                "classifier": "rule_based",
                "thresholds": {
                    "skill_match": self.skill_match_threshold,
                    "skill_partial": self.skill_partial_threshold,
                },
            },
        )

    # ------------------------------------------------------------------
    # Per-dimension scoring
    # ------------------------------------------------------------------

    def _score_role_alignment(
        self,
        seeker: Mapping[str, Any],
        vacancy: Mapping[str, Any],
    ) -> FitDimension:
        """MATCH = family match + a title-keyword hit; PARTIAL = family only; MISS = neither."""
        seeker_targets = seeker.get("role_targets") or []
        if not seeker_targets:
            return FitDimension.MISS

        vac_family = (vacancy.get("role_family") or "").strip().lower()
        vac_title = (vacancy.get("title") or "").strip().lower()
        if not vac_family:
            return FitDimension.MISS

        family_hit = False
        keyword_hit = False
        for target in seeker_targets:
            if not isinstance(target, Mapping):
                continue
            target_family = (target.get("role_family") or "").strip().lower()
            if target_family and target_family == vac_family:
                family_hit = True
                # Look for any role_title_keywords substring in the vacancy title.
                for kw in target.get("role_title_keywords") or []:
                    if kw and kw.strip().lower() in vac_title:
                        keyword_hit = True
                        break
            if keyword_hit:
                break

        if family_hit and keyword_hit:
            return FitDimension.MATCH
        if family_hit:
            return FitDimension.PARTIAL
        return FitDimension.MISS

    def _score_seniority_fit(
        self,
        seeker: Mapping[str, Any],
        vacancy: Mapping[str, Any],
    ) -> FitDimension:
        """MATCH = vacancy seniority within any target's range; PARTIAL = one ladder rung away; MISS = further."""
        vac_level = (vacancy.get("seniority_level") or "").strip().lower()
        if not vac_level or vac_level not in SENIORITY_LADDER:
            # Vacancy doesn't specify — treat as PARTIAL (don't punish unknowns).
            return FitDimension.PARTIAL
        vac_idx = SENIORITY_LADDER.index(vac_level)

        best_distance: Optional[int] = None
        for target in seeker.get("role_targets") or []:
            if not isinstance(target, Mapping):
                continue
            rng = target.get("seniority_range") or []
            indices = [SENIORITY_LADDER.index(s) for s in rng if s in SENIORITY_LADDER]
            if not indices:
                continue
            lo, hi = min(indices), max(indices)
            if lo <= vac_idx <= hi:
                return FitDimension.MATCH
            distance = min(abs(vac_idx - lo), abs(vac_idx - hi))
            if best_distance is None or distance < best_distance:
                best_distance = distance

        if best_distance is None:
            return FitDimension.PARTIAL  # no usable seniority info — unknown, not punished
        if best_distance == 1:
            return FitDimension.PARTIAL
        return FitDimension.MISS

    def _score_skill_overlap(
        self,
        seeker: Mapping[str, Any],
        vacancy: Mapping[str, Any],
    ) -> FitDimension:
        """Jaccard overlap on lowercased skill sets, thresholded.

        Nice-to-have skills count toward the vacancy set with full weight at
        this layer — the protocol doesn't distinguish their pull; classification
        granularity is coarse enough that nice-to-have hits are still signal.
        """
        seeker_skills = _normalised_set(seeker.get("skills"))
        vac_skills = _normalised_set(vacancy.get("skills"))
        vac_skills |= _normalised_set(vacancy.get("nice_to_have_skills"))

        if not seeker_skills or not vac_skills:
            return FitDimension.MISS

        intersection = seeker_skills & vac_skills
        union = seeker_skills | vac_skills
        if not union:
            return FitDimension.MISS
        jaccard = len(intersection) / len(union)
        if jaccard >= self.skill_match_threshold:
            return FitDimension.MATCH
        if jaccard >= self.skill_partial_threshold:
            return FitDimension.PARTIAL
        return FitDimension.MISS

    def _score_context_fit(
        self,
        seeker: Mapping[str, Any],
        vacancy: Mapping[str, Any],
    ) -> FitDimension:
        """Country + language + employment type alignment.

        Three sub-checks; MATCH = all three pass, PARTIAL = two of three, MISS = fewer.
        """
        country_ok = _check_country(seeker, vacancy)
        language_ok = _check_language(seeker, vacancy)
        employment_ok = _check_employment_type(seeker, vacancy)

        passes = sum(1 for ok in (country_ok, language_ok, employment_ok) if ok)
        if passes == 3:
            return FitDimension.MATCH
        if passes == 2:
            return FitDimension.PARTIAL
        return FitDimension.MISS

    # ------------------------------------------------------------------
    # Aggregation — turn four dimension grades into a verdict + reason.
    # ------------------------------------------------------------------

    def _aggregate(
        self,
        dimensions: Mapping[str, FitDimension],
        vacancy: Mapping[str, Any],
    ) -> tuple[FitVerdict, str]:
        role = dimensions["role_alignment"]
        seniority = dimensions["seniority_fit"]
        skill = dimensions["skill_overlap"]
        context = dimensions["context_fit"]

        # NO_FIT: role doesn't even share a family.
        if role == FitDimension.MISS:
            return FitVerdict.NO_FIT, "Role family does not overlap with your targets."

        # STRONG: every dimension matches.
        if (
            role == FitDimension.MATCH
            and seniority == FitDimension.MATCH
            and skill == FitDimension.MATCH
            and context == FitDimension.MATCH
        ):
            title = (vacancy.get("title") or "this role").strip()
            return (
                FitVerdict.STRONG,
                f"Strong fit on role, seniority, skills, and context for {title}.",
            )

        # WEAK: anything else with at least family overlap.
        miss_dims = [name for name, grade in dimensions.items() if grade == FitDimension.MISS]
        if miss_dims:
            human = ", ".join(d.replace("_", " ") for d in miss_dims)
            return FitVerdict.WEAK, f"Adjacent fit — gaps on {human}."
        return FitVerdict.WEAK, "Adjacent fit — overlap is partial across one or more dimensions."


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _normalised_set(items: Optional[Iterable[Any]]) -> set:
    """Lowercase + strip a list of strings into a set; drop non-strings."""
    if not items:
        return set()
    out = set()
    for item in items:
        if isinstance(item, str):
            cleaned = item.strip().lower()
            if cleaned:
                out.add(cleaned)
    return out


def _check_country(seeker: Mapping[str, Any], vacancy: Mapping[str, Any]) -> bool:
    """At least one vacancy country in seeker geography.countries (or seeker accepts global).

    Honours geography.scope == "global" as an explicit accept-anywhere signal,
    and respects geography.countries_excluded (added in v0.2.1).
    """
    geo = seeker.get("geography") or {}
    vac_countries = _normalised_set(vacancy.get("country_codes"))
    if not vac_countries and vacancy.get("country_code"):
        vac_countries = _normalised_set([vacancy["country_code"]])
    excluded = _normalised_set(geo.get("countries_excluded"))
    vac_countries -= excluded
    if not vac_countries:
        return False
    if (geo.get("scope") or "regions").lower() == "global":
        return True
    seeker_countries = _normalised_set(geo.get("countries"))
    if not seeker_countries:
        return False
    return bool(seeker_countries & vac_countries)


def _check_language(seeker: Mapping[str, Any], vacancy: Mapping[str, Any]) -> bool:
    """Seeker has at least the required CEFR level for every required vacancy language.

    CEFR ladder A1<A2<B1<B2<C1<C2. A seeker language without a level is treated as B2
    (a reasonable working assumption — the protocol's language gate upstream of the
    validator is already in play; this is a belt-and-braces check).
    """
    required = vacancy.get("languages_required") or []
    if not required:
        return True
    seeker_langs = {}
    for entry in seeker.get("languages") or []:
        if not isinstance(entry, Mapping):
            continue
        lang = (entry.get("language") or "").lower()
        level = (entry.get("level") or "B2").upper()
        if lang:
            seeker_langs[lang] = level

    for entry in required:
        if not isinstance(entry, Mapping):
            continue
        lang = (entry.get("language") or "").lower()
        required_level = (entry.get("level") or "B2").upper()
        speaker_level = seeker_langs.get(lang)
        if not speaker_level:
            return False
        if _cefr_rank(speaker_level) < _cefr_rank(required_level):
            return False
    return True


_CEFR_LADDER = ("A1", "A2", "B1", "B2", "C1", "C2")


def _cefr_rank(level: str) -> int:
    level = (level or "").upper().strip()
    if level in _CEFR_LADDER:
        return _CEFR_LADDER.index(level)
    return -1


def _check_employment_type(seeker: Mapping[str, Any], vacancy: Mapping[str, Any]) -> bool:
    """Vacancy employment_type appears in seeker.employment_types."""
    vac_type = (vacancy.get("employment_type") or "").strip().lower()
    if not vac_type:
        return True
    seeker_types = _normalised_set(seeker.get("employment_types"))
    if not seeker_types:
        return True  # seeker didn't restrict
    return vac_type in seeker_types
