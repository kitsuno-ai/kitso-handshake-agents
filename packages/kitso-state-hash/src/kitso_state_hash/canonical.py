"""Canonical subset extractors + state_hash entry points.

Implements §2.3 and §2.4 of the spec, plus the §2.4.1 normative split-shape
languages flattening.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .jcs import canonical_bytes

# CEFR precedence for §2.4.1 highest-level-wins when a language repeats in `speaks`.
# `Native` outranks all CEFR levels; lower-cased values are accepted on input
# for robustness, normalised to their canonical case in output.
_CEFR_ORDER = {
    "native": 7,
    "c2": 6,
    "c1": 5,
    "b2": 4,
    "b1": 3,
    "a2": 2,
    "a1": 1,
}
_CEFR_CANONICAL_CASE = {
    "native": "Native",
    "c2": "C2",
    "c1": "C1",
    "b2": "B2",
    "b1": "B1",
    "a2": "A2",
    "a1": "A1",
}


# ── §2.3: Vacancy canonical subset ─────────────────────────────────────────

_VACANCY_FIELDS = (
    "card_id",
    "country_codes",
    "employment_type",
    "languages_required",
    "nice_to_have_skills",
    "remote_type",
    "role_family",
    "salary_currency",
    "salary_max",
    "salary_min",
    "salary_period",
    "seniority_level",
    "skills",
    "visa_sponsorship_offered",
    "years_experience_min",
)

_VACANCY_SORTED_STRING_LISTS = frozenset({
    "country_codes", "nice_to_have_skills", "skills",
})


def canonical_subset_vacancy(card: dict) -> dict:
    """Return the §2.3 canonical subset of a vacancy card.

    Missing optional fields are omitted (not emitted as null). Lists of
    strings are sorted alphabetically. `languages_required` (a list of
    {language, level} objects) is sorted by `language`.
    """
    out: dict = {}
    for k in _VACANCY_FIELDS:
        if k not in card:
            continue
        v = card[k]
        if v is None:
            continue
        if k in _VACANCY_SORTED_STRING_LISTS:
            v = sorted(str(x) for x in (v or []))
        elif k == "languages_required":
            v = _sort_language_level_list(v)
        out[k] = v
    return out


def vacancy_state_hash(card: dict) -> str:
    """Compute the 64-char lowercase hex SHA-256 state hash of a vacancy card."""
    subset = canonical_subset_vacancy(card)
    return hashlib.sha256(canonical_bytes(subset)).hexdigest()


# ── §2.4: Seeker canonical subset (+ §2.4.1 languages flatten) ─────────────

_SEEKER_FIELDS = (
    "card_id",
    "employment_types",
    "geography",          # only .countries reaches the hash; see _project_seeker_geography
    "languages",
    "role_targets",
    "salary_expectation", # only .min.{amount,currency,period}
    "skills",
    "work_mode",          # accepts (sorted) + max_onsite_days_per_week
    "work_rights",        # countries_authorized (sorted) + sponsorship_required
)

_SEEKER_SORTED_STRING_LISTS = frozenset({
    "employment_types", "skills",
})


def canonical_subset_seeker(card: dict) -> dict:
    """Return the §2.4 canonical subset of a seeker card.

    Handles the §2.4.1 languages split-shape flattening: if `languages` is
    a `{speaks, works_in}` dict, it is flattened to the canonical
    `[{language, level}, ...]` list, sorted by `language`, deduplicated
    with highest-CEFR-level wins. `works_in` does NOT participate in the
    hash (per §2.4.1 rule 3).
    """
    out: dict = {}

    if "card_id" in card and card["card_id"] is not None:
        out["card_id"] = card["card_id"]

    if "employment_types" in card and card["employment_types"]:
        out["employment_types"] = sorted(str(x) for x in card["employment_types"])

    geo = card.get("geography")
    if isinstance(geo, dict) and geo.get("countries"):
        out["geography"] = {"countries": sorted(str(x) for x in geo["countries"])}

    if "languages" in card and card["languages"] is not None:
        out["languages"] = _canonicalize_seeker_languages(card["languages"])

    if "role_targets" in card and card["role_targets"]:
        out["role_targets"] = _canonicalize_role_targets(card["role_targets"])

    se = card.get("salary_expectation")
    if isinstance(se, dict):
        min_obj = se.get("min")
        if isinstance(min_obj, dict):
            sub = {}
            for k in ("amount", "currency", "period"):
                if k in min_obj and min_obj[k] is not None:
                    sub[k] = min_obj[k]
            if sub:
                out["salary_expectation"] = {"min": sub}

    if "skills" in card and card["skills"]:
        out["skills"] = sorted(str(x) for x in card["skills"])

    wm = card.get("work_mode")
    if isinstance(wm, dict):
        sub = {}
        if wm.get("accepts"):
            sub["accepts"] = sorted(str(x) for x in wm["accepts"])
        if "max_onsite_days_per_week" in wm and wm["max_onsite_days_per_week"] is not None:
            sub["max_onsite_days_per_week"] = wm["max_onsite_days_per_week"]
        if sub:
            out["work_mode"] = sub

    wr = card.get("work_rights")
    if isinstance(wr, dict):
        sub = {}
        if wr.get("countries_authorized"):
            sub["countries_authorized"] = sorted(str(x) for x in wr["countries_authorized"])
        if "sponsorship_required" in wr and wr["sponsorship_required"] is not None:
            sub["sponsorship_required"] = wr["sponsorship_required"]
        if sub:
            out["work_rights"] = sub

    return out


def seeker_state_hash(card: dict) -> str:
    """Compute the 64-char lowercase hex SHA-256 state hash of a seeker card."""
    subset = canonical_subset_seeker(card)
    return hashlib.sha256(canonical_bytes(subset)).hexdigest()


# ── Helpers ────────────────────────────────────────────────────────────────

def _sort_language_level_list(v: Any) -> list:
    """Sort a [{language, level}, ...] list by `language` (lowercase).

    Tolerates partial entries and string-only entries; the latter become
    `{language: <code>, level: None}` and are sorted by code.
    """
    if not isinstance(v, list):
        return []
    normalised = []
    for entry in v:
        if isinstance(entry, dict):
            lang = str(entry.get("language", "")).strip()
            level = entry.get("level")
            if lang:
                normalised.append({"language": lang.lower(), "level": level})
        elif isinstance(entry, str) and entry.strip():
            normalised.append({"language": entry.strip().lower(), "level": None})
    normalised.sort(key=lambda e: e["language"])
    # Strip None level so JCS doesn't emit null for missing optional info.
    return [
        {"language": e["language"], "level": e["level"]} if e["level"] is not None
        else {"language": e["language"]}
        for e in normalised
    ]


def _canonicalize_seeker_languages(value: Any) -> list:
    """§2.4.1: flatten split-shape `{speaks, works_in}` or pass through legacy
    list shape, normalising to the canonical `[{language, level}, ...]` form.

    Output is sorted by `language` (lowercase), de-duplicated by language with
    highest-CEFR-level wins. `works_in` (if present) is dropped — it is an
    internal optimization for work-mode gating and does NOT participate in
    the published canonical shape per §2.4.1 rule 3.
    """
    if isinstance(value, dict) and "speaks" in value:
        speaks = value.get("speaks") or []
    elif isinstance(value, list):
        speaks = value
    else:
        return []

    # Collect (lang_lower, level_canonical_or_None) tuples, taking highest level on dup
    best: dict[str, str | None] = {}
    for entry in speaks:
        if isinstance(entry, dict):
            lang = str(entry.get("language", "")).strip().lower()
            level_raw = entry.get("level")
        elif isinstance(entry, str):
            lang = entry.strip().lower()
            level_raw = None
        else:
            continue
        if not lang:
            continue

        level_key = (level_raw or "").strip().lower() if isinstance(level_raw, str) else None
        level_rank = _CEFR_ORDER.get(level_key or "", 0)
        level_canonical = _CEFR_CANONICAL_CASE.get(level_key) if level_key in _CEFR_CANONICAL_CASE else None

        if lang in best:
            prev = best[lang]
            prev_rank = _CEFR_ORDER.get((prev or "").lower() if prev else "", 0)
            if level_rank > prev_rank:
                best[lang] = level_canonical
        else:
            best[lang] = level_canonical

    out = []
    for lang in sorted(best.keys()):
        if best[lang] is not None:
            out.append({"language": lang, "level": best[lang]})
        else:
            out.append({"language": lang})
    return out


def _canonicalize_role_targets(targets: Any) -> list:
    """§2.4 role_targets: each entry has role_family, seniority_range (sorted),
    role_title_keywords (sorted); outer list sorted by role_family.
    """
    if not isinstance(targets, list):
        return []
    out = []
    for t in targets:
        if not isinstance(t, dict):
            continue
        rf = t.get("role_family")
        if not isinstance(rf, str) or not rf.strip():
            continue
        entry = {"role_family": rf.strip()}
        if "seniority_range" in t and t["seniority_range"]:
            entry["seniority_range"] = sorted(str(x) for x in t["seniority_range"])
        if "role_title_keywords" in t and t["role_title_keywords"]:
            entry["role_title_keywords"] = sorted(str(x) for x in t["role_title_keywords"])
        out.append(entry)
    out.sort(key=lambda e: e["role_family"])
    return out
