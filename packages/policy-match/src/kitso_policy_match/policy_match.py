"""policy_match.py — handshake policy evaluator (S313).

Pure function, no DB, no network, sub-millisecond.

Spec reference: https://github.com/kitsuno-ai/kitso-handshake-agents/blob/main/docs/protocol-v0.2.md  (§5.5)

The evaluator pairs both sides field-by-field using a shared vocabulary (§5.1)
and a closed operator set (§5.3). It runs symmetrically: vacancy's criteria
check against seeker's traits, seeker's criteria check against vacancy's traits.

Gate semantics (§5.2):
  hard           — mismatch blocks the handshake at the current stage
  soft           — mismatch records as below-threshold; blocks at L2 only
  informational  — mismatch never blocks; recorded for transparency

Stage semantics (§5.5):
  stage="L1"  → only hard mismatches block, soft are recorded
  stage="L2"  → both hard AND soft mismatches block

Missing trait policy:
  If a criterion references a field the other side has not declared, and the
  operator is not `present`/`absent`, the criterion evaluates to "unknown".
  Hard-gate unknowns block at L1 (strict default — don't fire on uncertainty);
  soft-gate unknowns record as soft-fail (don't block L1, block at L2).

This module has zero imports outside the Python stdlib so it can be unit-tested
in isolation and embedded anywhere the seeker or the dashboard runs.
"""

from __future__ import annotations

from typing import Any, Iterable

# ── Region taxonomy ──────────────────────────────────────────────────────────
# Mirror of the EU/EFTA expansion already used in dashboard/score_worker.py.
# Kept inline here so policy_match.py has no internal imports.

_EU_COUNTRIES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE",
})
_EFTA_COUNTRIES = frozenset({"CH", "NO", "IS", "LI"})

_REGION_TAG_EXPANSIONS: dict[str, frozenset[str]] = {
    "EU": _EU_COUNTRIES,
    "EFTA": _EFTA_COUNTRIES,
    "EEA": _EU_COUNTRIES | frozenset({"NO", "IS", "LI"}),
}

# Fields where EU/EFTA tags should auto-expand to ISO-2 sets
_REGION_AWARE_FIELDS = frozenset({"work_rights", "country", "country_codes", "city"})

# ── Outcome constants ────────────────────────────────────────────────────────
FIRE_L1 = "FIRE_L1"
BLOCK_L1 = "BLOCK_L1"
ELIGIBLE_L2 = "ELIGIBLE_L2"
BLOCK_L2 = "BLOCK_L2"

# Criterion outcomes
MATCH = "match"
MISMATCH = "mismatch"
UNKNOWN = "unknown"

VALID_OPERATORS = frozenset({
    "in", "not_in", "any", "all", "gte", "lte", "equals", "present", "absent",
})
VALID_GATES = frozenset({"hard", "soft", "informational"})


# ── Helpers ──────────────────────────────────────────────────────────────────
def _expand_region_tags(values: Iterable[Any]) -> set[str]:
    """Replace EU/EFTA/EEA tags with their ISO-2 country code sets.
    All other values are upper-cased if they look like ISO codes (length ≤ 3).
    """
    out: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        if v in _REGION_TAG_EXPANSIONS:
            out.update(_REGION_TAG_EXPANSIONS[v])
        elif len(v) <= 3:
            out.add(v.upper())
        else:
            out.add(v.lower())
    return out


# Legacy trait name aliases (S337 — 30d deprecation window)
_TRAIT_LEGACY_ALIASES = {
    "work_permit": "work_rights.countries_authorized",
    "salary_min": "salary_expectation.min.amount",
}


def _get_trait(traits: Any, field: str) -> Any:
    """Read a trait from a dict or attribute-bearing object. 
    
    Supports dotted-path resolution (e.g. "work_rights.countries_authorized").
    Legacy flat names are mapped to canonical paths during deprecation window.
    Returns None if absent.
    """
    if traits is None:
        return None
    
    # Check for legacy alias
    if field in _TRAIT_LEGACY_ALIASES:
        canonical_field = _TRAIT_LEGACY_ALIASES[field]
        print(f"[DEPRECATION WARNING] Trait '{field}' is deprecated, use '{canonical_field}' instead")
        field = canonical_field
    
    # Dotted-path resolution: "work_rights.countries_authorized" → traits["work_rights"]["countries_authorized"]
    if "." in field:
        parts = field.split(".")
        current = traits
        
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part, None)
            
            if current is None:
                return None
        
        return current
    
    # Simple field access (backward compatibility)
    if isinstance(traits, dict):
        return traits.get(field)
    return getattr(traits, field, None)


# Fields whose trait value is a split-shape dict: criteria evaluate against
# a specific inner key, not the dict itself. Currently only `languages` —
# {speaks: [...], works_in: [...]} — where `works_in` is the work-language gate.
_TRAIT_SPLIT_INNER = {
    "languages": "works_in",
}


def _unwrap_split_trait(value: Any, field: str) -> Any:
    """If `value` is the S345 split-shape dict for this field, return the inner
    gate list. Otherwise pass through unchanged so legacy list/string shapes
    continue to work."""
    inner = _TRAIT_SPLIT_INNER.get(field)
    if not inner:
        return value
    if isinstance(value, dict) and inner in value:
        return value.get(inner) or []
    return value


def _normalize_trait_to_set(value: Any, field: str) -> set[str]:
    """Coerce a trait value into a set of comparable tokens.
    Region-aware fields get EU/EFTA expansion applied symmetrically.
    """
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        items = [value]
    items = [str(x) for x in items if x is not None]
    if field in _REGION_AWARE_FIELDS:
        return _expand_region_tags(items)
    return {x.lower() if isinstance(x, str) else x for x in items}


def _normalize_criterion_values(values: Any, field: str) -> set[str]:
    """Same coercion for the values declared in a criterion."""
    if not values:
        return set()
    if not isinstance(values, (list, tuple, set, frozenset)):
        values = [values]
    items = [str(x) for x in values if x is not None]
    if field in _REGION_AWARE_FIELDS:
        return _expand_region_tags(items)
    return {x.lower() if isinstance(x, str) else x for x in items}


def _trait_is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, set, frozenset, dict, str)) and len(value) == 0:
        return True
    return False


# ── Criterion evaluation ─────────────────────────────────────────────────────
def _evaluate_criterion(criterion: dict, traits: Any) -> dict:
    """Evaluate one criterion against the other side's traits.

    Returns a dict {field, operator, gate, outcome, reason?} where outcome is
    one of MATCH, MISMATCH, UNKNOWN.
    """
    field = criterion.get("field")
    op = criterion.get("operator")
    gate = criterion.get("gate", "soft")
    values = criterion.get("values")
    value = criterion.get("value")

    result: dict = {"field": field, "operator": op, "gate": gate}

    # Schema sanity
    if gate not in VALID_GATES:
        return {**result, "outcome": UNKNOWN, "reason": f"invalid gate: {gate}"}
    if op not in VALID_OPERATORS:
        return {**result, "outcome": UNKNOWN, "reason": f"unknown operator: {op}"}
    if not field:
        return {**result, "outcome": UNKNOWN, "reason": "criterion has no field"}

    trait_val = _get_trait(traits, field)
    # S345: unwrap split-shape traits (e.g. languages -> works_in) before
    # any operator runs. Pure pass-through for non-split fields.
    trait_val = _unwrap_split_trait(trait_val, field)

    # ── present / absent ─────────────────────────────────────────
    if op == "present":
        if _trait_is_empty(trait_val):
            return {**result, "outcome": MISMATCH,
                    "reason": f"required trait '{field}' not declared"}
        return {**result, "outcome": MATCH}

    if op == "absent":
        if _trait_is_empty(trait_val):
            return {**result, "outcome": MATCH}
        return {**result, "outcome": MISMATCH,
                "reason": f"trait '{field}' must be absent, got {trait_val!r}"}

    # ── unknown trait (any other op) ─────────────────────────────
    if _trait_is_empty(trait_val):
        return {**result, "outcome": UNKNOWN,
                "reason": f"trait '{field}' not declared"}

    # ── numeric comparisons ──────────────────────────────────────
    if op in ("gte", "lte"):
        try:
            t = float(trait_val)
            v = float(value)
        except (TypeError, ValueError):
            return {**result, "outcome": UNKNOWN,
                    "reason": f"cannot numerically compare {field}={trait_val!r} to {value!r}"}
        ok = (t >= v) if op == "gte" else (t <= v)
        if ok:
            return {**result, "outcome": MATCH}
        cmp = "<" if op == "gte" else ">"
        bound = "required" if op == "gte" else "allowed"
        return {**result, "outcome": MISMATCH,
                "reason": f"{field}={t} {cmp} {bound} {v}"}

    if op == "equals":
        if str(trait_val).lower() == str(value).lower():
            return {**result, "outcome": MATCH}
        return {**result, "outcome": MISMATCH,
                "reason": f"{field}={trait_val!r} != {value!r}"}

    # ── set operations ───────────────────────────────────────────
    trait_set = _normalize_trait_to_set(trait_val, field)
    crit_set = _normalize_criterion_values(values, field)

    if not crit_set:
        return {**result, "outcome": UNKNOWN, "reason": "criterion has no values"}

    if op == "in":
        if trait_set & crit_set:
            return {**result, "outcome": MATCH}
        return {**result, "outcome": MISMATCH,
                "reason": f"{field}={sorted(trait_set)} ∉ {sorted(crit_set)}"}

    if op == "not_in":
        overlap = trait_set & crit_set
        if not overlap:
            return {**result, "outcome": MATCH}
        return {**result, "outcome": MISMATCH,
                "reason": f"{field} contains disallowed: {sorted(overlap)}"}

    if op == "any":
        # min_matches >= 1 (default 1) — caller can require multiple overlaps
        overlap = trait_set & crit_set
        min_matches = criterion.get("min_matches", 1)
        try:
            min_matches = int(min_matches)
        except (TypeError, ValueError):
            min_matches = 1
        if len(overlap) >= min_matches:
            return {**result, "outcome": MATCH,
                    "reason": f"{len(overlap)} of required {min_matches}: {sorted(overlap)}"}
        return {**result, "outcome": MISMATCH,
                "reason": f"only {len(overlap)} of required {min_matches} in seeker's {field} (overlap={sorted(overlap)})"}

    if op == "all":
        missing = crit_set - trait_set
        if not missing:
            return {**result, "outcome": MATCH}
        return {**result, "outcome": MISMATCH,
                "reason": f"{field} missing required: {sorted(missing)}"}

    # Defensive — should be unreachable given VALID_OPERATORS gate
    return {**result, "outcome": UNKNOWN, "reason": f"unhandled operator: {op}"}


# ── Public entry point ───────────────────────────────────────────────────────
def evaluate(
    card_policy: dict | None,
    card_traits: dict | None,
    seeker_policy: dict | None,
    seeker_traits: dict | None,
    stage: str = "L1",
) -> dict:
    """Run a symmetric handshake evaluation.

    Args:
      card_policy:    vacancy's criteria. None or {"criteria": []} = unfiltered.
      card_traits:    vacancy's own traits (role_family, country_code, etc.).
      seeker_policy:  seeker's criteria. None or empty = no seeker filters.
      seeker_traits:  seeker's own traits (work_rights, languages, etc.).
      stage:          "L1" (initial) or "L2" (post-vacancy-signal escalation).

    Returns dict:
      {
        outcome:           FIRE_L1 | BLOCK_L1 | ELIGIBLE_L2 | BLOCK_L2,
        stage:             "L1" | "L2",
        matched_criteria:  list of evaluated criteria with side+outcome+reason,
        blocking_criteria: subset that blocks at this stage,
      }

    Stored in handshake_conversations.policy_decision_trace JSONB.
    """
    if stage not in ("L1", "L2"):
        raise ValueError(f"stage must be 'L1' or 'L2', got {stage!r}")

    matched: list[dict] = []

    # Vacancy → Seeker
    if card_policy and isinstance(card_policy, dict):
        for c in card_policy.get("criteria", []) or []:
            r = _evaluate_criterion(c, seeker_traits or {})
            r["side"] = "vacancy_requires"
            matched.append(r)

    # Seeker → Vacancy
    if seeker_policy and isinstance(seeker_policy, dict):
        for c in seeker_policy.get("criteria", []) or []:
            r = _evaluate_criterion(c, card_traits or {})
            r["side"] = "seeker_requires"
            matched.append(r)

    # Gate filter by stage
    blocking_outcomes = {MISMATCH, UNKNOWN}
    if stage == "L1":
        blocking_gates = {"hard"}
    else:
        blocking_gates = {"hard", "soft"}

    blocking = [
        m for m in matched
        if m["gate"] in blocking_gates and m["outcome"] in blocking_outcomes
    ]

    if stage == "L1":
        outcome = BLOCK_L1 if blocking else FIRE_L1
    else:
        outcome = BLOCK_L2 if blocking else ELIGIBLE_L2

    return {
        "outcome": outcome,
        "stage": stage,
        "matched_criteria": matched,
        "blocking_criteria": blocking,
    }
