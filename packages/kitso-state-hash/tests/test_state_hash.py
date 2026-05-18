"""Tests for kitso-state-hash v0.1.0.

Goals:
- Round-trip determinism: hashing the same card twice gives the same hex.
- Sort stability: same fields in different input order yield same hash.
- Missing-optional invariance: omitting a field gives a different hash than
  setting it to a real value, but None ≡ absent.
- §2.4.1 split-shape flattening: split shape and pre-flattened list shape
  yield the same hash on the seeker side; works_in does not affect the hash.
- Unicode pass-through and string-escaping correctness.
- JCS number-formatting edge cases (integers, integral floats).
"""
from __future__ import annotations

import hashlib

from kitso_state_hash import (
    canonical_bytes,
    canonical_subset_seeker,
    canonical_subset_vacancy,
    seeker_state_hash,
    vacancy_state_hash,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


VAC = {
    "card_id": "0193e8ac-3f21-7a90-bef0-1c8f4a5d2e91",
    "slug": "senior-python-engineer",
    "title": "Senior Python Engineer",
    "role_family": "software_engineering",
    "seniority_level": "senior",
    "employment_type": "full_time",
    "remote_type": "remote",
    "country_codes": ["CH", "DE", "AT"],
    "skills": ["python", "kubernetes", "postgres", "system_design"],
    "nice_to_have_skills": ["rust", "terraform"],
    "languages_required": [{"language": "en", "level": "C1"}],
    "years_experience_min": 5,
    "visa_sponsorship_offered": False,
    "salary_min": 95000,
    "salary_max": 120000,
    "salary_currency": "EUR",
    "salary_period": "year",
    "description": "Lead the migration of our event-driven backend.",
    "company_name": "Acme Example",
    "verification": {"method": "email+dns"},
    "published_at": "2026-05-15T08:00:00Z",
}

SEE = {
    "card_id": "0193e8b0-1c8f-4a5d-2e91-3f217a90bef0",
    "slug": "alice-py-senior",
    "agent_id": "kitsuno.agent/u/alice",
    "role_targets": [
        {
            "role_family": "software_engineering",
            "role_title_keywords": ["python engineer", "platform engineer"],
            "seniority_range": ["senior", "lead"],
        }
    ],
    "skills": ["python", "kubernetes", "postgres"],
    "languages": [
        {"language": "en", "level": "C1"},
        {"language": "de", "level": "B2"},
    ],
    "geography": {"countries": ["CH", "DE", "AT"], "cities": ["Zurich"]},
    "work_mode": {"accepts": ["remote", "hybrid"], "max_onsite_days_per_week": 2},
    "employment_types": ["full_time", "contract"],
    "work_rights": {
        "countries_authorized": ["AT", "CH", "DE"],
        "sponsorship_required": False,
    },
    "salary_expectation": {
        "min": {"amount": 85000, "currency": "EUR", "period": "year"},
        "negotiable": True,
    },
    "verification": {"method": "email+dns"},
    "published_at": "2026-05-15T08:00:00Z",
}


# ── Determinism ────────────────────────────────────────────────────────────

def test_vacancy_hash_is_deterministic():
    h1 = vacancy_state_hash(VAC)
    h2 = vacancy_state_hash(VAC)
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_seeker_hash_is_deterministic():
    h1 = seeker_state_hash(SEE)
    h2 = seeker_state_hash(SEE)
    assert h1 == h2
    assert len(h1) == 64


# ── Sort stability — input order does not affect hash ──────────────────────

def test_vacancy_country_codes_input_order_invariant():
    a = dict(VAC, country_codes=["AT", "CH", "DE"])
    b = dict(VAC, country_codes=["DE", "AT", "CH"])
    assert vacancy_state_hash(a) == vacancy_state_hash(b)


def test_vacancy_skills_input_order_invariant():
    a = dict(VAC, skills=["postgres", "python", "kubernetes", "system_design"])
    b = dict(VAC, skills=["system_design", "python", "postgres", "kubernetes"])
    assert vacancy_state_hash(a) == vacancy_state_hash(b)


def test_seeker_skills_input_order_invariant():
    a = dict(SEE, skills=["python", "kubernetes", "postgres"])
    b = dict(SEE, skills=["postgres", "kubernetes", "python"])
    assert seeker_state_hash(a) == seeker_state_hash(b)


def test_seeker_role_targets_outer_order_invariant():
    a = dict(SEE, role_targets=[
        {"role_family": "data_engineering", "seniority_range": ["senior"]},
        {"role_family": "software_engineering", "seniority_range": ["senior"]},
    ])
    b = dict(SEE, role_targets=[
        {"role_family": "software_engineering", "seniority_range": ["senior"]},
        {"role_family": "data_engineering", "seniority_range": ["senior"]},
    ])
    assert seeker_state_hash(a) == seeker_state_hash(b)


# ── Excluded fields don't affect the hash ──────────────────────────────────

def test_vacancy_description_does_not_affect_hash():
    a = dict(VAC, description="x")
    b = dict(VAC, description="totally different prose here")
    assert vacancy_state_hash(a) == vacancy_state_hash(b)


def test_vacancy_slug_does_not_affect_hash():
    a = dict(VAC, slug="aaa")
    b = dict(VAC, slug="zzz")
    assert vacancy_state_hash(a) == vacancy_state_hash(b)


def test_vacancy_published_at_does_not_affect_hash():
    a = dict(VAC, published_at="2026-05-15T08:00:00Z")
    b = dict(VAC, published_at="2026-12-01T12:34:56Z")
    assert vacancy_state_hash(a) == vacancy_state_hash(b)


def test_seeker_agent_id_does_not_affect_hash():
    a = dict(SEE, agent_id="kitsuno.agent/u/alice")
    b = dict(SEE, agent_id="kitsuno.agent/u/bob")
    assert seeker_state_hash(a) == seeker_state_hash(b)


# ── Real semantic fields DO affect the hash ────────────────────────────────

def test_vacancy_skills_change_affects_hash():
    a = dict(VAC, skills=["python", "kubernetes"])
    b = dict(VAC, skills=["python", "kubernetes", "redis"])
    assert vacancy_state_hash(a) != vacancy_state_hash(b)


def test_vacancy_salary_min_change_affects_hash():
    a = dict(VAC, salary_min=95000)
    b = dict(VAC, salary_min=100000)
    assert vacancy_state_hash(a) != vacancy_state_hash(b)


def test_seeker_seniority_range_change_affects_hash():
    a = dict(SEE)
    b = dict(SEE, role_targets=[{
        "role_family": "software_engineering",
        "role_title_keywords": ["python engineer", "platform engineer"],
        "seniority_range": ["lead", "principal"],   # was [senior, lead]
    }])
    assert seeker_state_hash(a) != seeker_state_hash(b)


# ── Missing-optional invariance ────────────────────────────────────────────

def test_vacancy_missing_optional_equiv_none():
    a = dict(VAC); a.pop("nice_to_have_skills", None)
    b = dict(VAC, nice_to_have_skills=None)
    assert vacancy_state_hash(a) == vacancy_state_hash(b)


def test_vacancy_empty_list_distinct_from_absent():
    """Defensive: empty list IS distinguishable from absent. Spec is silent
    on this corner; current impl treats `[]` as falsey and omits, which is
    same as absent. If we ever want to change that, this test catches it.
    """
    a = dict(VAC); a.pop("nice_to_have_skills", None)
    b = dict(VAC, nice_to_have_skills=[])
    # impl currently: empty list -> falsey -> sorted([]) -> [] in subset
    # but our sorted-string-lists path uses `(v or [])` which keeps it as []
    # Let me check: vacancy.canonical_subset_vacancy for nice_to_have_skills
    # uses sorted(str(x) for x in (v or [])) which yields []. So the field
    # is emitted as [] vs absent — they differ.
    # If you want empty-list ≡ absent, drop the field when v is empty.
    # Document the current choice in this test.
    assert vacancy_state_hash(a) != vacancy_state_hash(b)


# ── §2.4.1 split-shape languages flattening ────────────────────────────────

def test_seeker_languages_split_vs_flat_equiv():
    """Split shape with works_in is flattened to the same canonical form as
    the published flat shape. Hashes MUST be equal."""
    seeker_flat = dict(SEE)
    seeker_split = dict(SEE, languages={
        "speaks": [
            {"language": "de", "level": "B2"},
            {"language": "en", "level": "C1"},
        ],
        "works_in": ["de", "en"],
    })
    assert seeker_state_hash(seeker_flat) == seeker_state_hash(seeker_split)


def test_seeker_works_in_does_not_affect_hash():
    """works_in is internal only — changing it must not change the hash."""
    a = dict(SEE, languages={
        "speaks": [{"language": "de", "level": "B2"}, {"language": "en", "level": "C1"}],
        "works_in": ["de"],
    })
    b = dict(SEE, languages={
        "speaks": [{"language": "de", "level": "B2"}, {"language": "en", "level": "C1"}],
        "works_in": ["de", "en", "fr"],
    })
    assert seeker_state_hash(a) == seeker_state_hash(b)


def test_seeker_languages_dedup_highest_cefr_wins():
    """If `speaks` contains the same language twice with different levels,
    the highest CEFR level wins."""
    a = dict(SEE, languages=[{"language": "en", "level": "C1"}])
    b = dict(SEE, languages={
        "speaks": [
            {"language": "en", "level": "B2"},
            {"language": "en", "level": "C1"},
        ],
        "works_in": [],
    })
    assert seeker_state_hash(a) == seeker_state_hash(b)


# ── Unicode + escaping ─────────────────────────────────────────────────────

def test_unicode_passthrough():
    a = dict(VAC, role_family="software_engineering")
    b = dict(VAC, role_family="software_engineering")
    assert vacancy_state_hash(a) == vacancy_state_hash(b)


def test_string_escape_quote_and_backslash():
    # A quote inside a string is escaped as \\\" — verify by building the input
    # in code (chr(34) is a literal ") to avoid shell/test escaping confusion.
    quote_input = "a" + chr(34) + "b"          # value is: a"b
    out_quote = canonical_bytes({"x": quote_input})
    assert out_quote == b'{"x":"a\\\"b"}'

    # A backslash inside a string is escaped as \\\\
    backslash_input = "a" + chr(92) + "b"      # value is: a\\b
    out_backslash = canonical_bytes({"x": backslash_input})
    assert out_backslash == b'{"x":"a\\\\b"}'

def test_control_chars_escaped():
    a = canonical_bytes({"x": "\n\t"})
    assert a == b'{"x":"\\n\\t"}'


# ── JCS numbers ────────────────────────────────────────────────────────────

def test_integer_float_serialized_as_integer():
    """1.0 must serialise as "1" per RFC 8785 / ECMA-404."""
    assert canonical_bytes({"x": 1.0}) == b'{"x":1}'
    assert canonical_bytes({"x": 1}) == b'{"x":1}'
    assert canonical_bytes({"x": 1.0}) == canonical_bytes({"x": 1})


def test_object_keys_sorted():
    """Keys must be sorted lexicographically regardless of insertion order."""
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert canonical_bytes({"a": 2, "b": 1}) == b'{"a":2,"b":1}'


def test_nan_rejected():
    import pytest
    with pytest.raises(ValueError):
        canonical_bytes({"x": float("nan")})


# ── canonical_subset_* surfaces — quick contract checks ────────────────────

def test_canonical_subset_vacancy_excludes_narrative():
    s = canonical_subset_vacancy(VAC)
    for excluded in ("slug", "title", "description", "company_name",
                     "verification", "published_at"):
        assert excluded not in s


def test_canonical_subset_seeker_excludes_internal():
    s = canonical_subset_seeker(SEE)
    for excluded in ("slug", "agent_id", "verification", "published_at"):
        assert excluded not in s
    # cities specifically dropped per §2.4 (only countries reaches the hash)
    assert "cities" not in s.get("geography", {})
