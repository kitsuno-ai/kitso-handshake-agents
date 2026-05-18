"""Tests for the rule-based reference validator.

These tests use the canonical v0.2 fixtures from ../../test-fixtures/v0.2/ so
that any future schema clean-up of those fixtures must also keep these tests
green — a useful tripwire.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from handshake_validator import (
    FitDimension,
    FitVerdict,
    RuleBasedValidator,
    Verdict,
)


FIXTURE_DIR = Path(__file__).parent.parent.parent.parent / "test-fixtures" / "v0.2"


def _load(name: str) -> dict:
    with open(FIXTURE_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def seeker_engineering() -> dict:
    return _load("seeker-card-engineering.json")


@pytest.fixture
def vacancy_direct_hire() -> dict:
    return _load("vacancy-card-direct-hire.json")


@pytest.fixture
def validator() -> RuleBasedValidator:
    return RuleBasedValidator()


# ----------------------------------------------------------------------
# Aligned pair — should classify as STRONG.
# ----------------------------------------------------------------------


def test_aligned_pair_is_strong_fit(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    """The fixtures are intentionally aligned (same skills, family, seniority, country).

    A baseline validator must reach STRONG on a textbook fit. If this test
    starts failing after a threshold tune, the threshold tune is too strict.
    """
    verdict = validator.validate(seeker_engineering, vacancy_direct_hire)
    assert verdict.verdict == FitVerdict.STRONG
    assert verdict.fit_dimensions["role_alignment"] == FitDimension.MATCH
    assert verdict.fit_dimensions["seniority_fit"] == FitDimension.MATCH
    assert verdict.fit_dimensions["skill_overlap"] == FitDimension.MATCH
    assert verdict.fit_dimensions["context_fit"] == FitDimension.MATCH


# ----------------------------------------------------------------------
# Role family mismatch — should be NO_FIT.
# ----------------------------------------------------------------------


def test_wrong_role_family_is_no_fit(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    """A SAP consultant for a software engineer seeker is NO_FIT regardless of country."""
    vac = copy.deepcopy(vacancy_direct_hire)
    vac["role_family"] = "consulting_strategy"
    vac["title"] = "SAP MM Consultant"

    verdict = validator.validate(seeker_engineering, vac)
    assert verdict.verdict == FitVerdict.NO_FIT
    assert verdict.fit_dimensions["role_alignment"] == FitDimension.MISS


# ----------------------------------------------------------------------
# Family match but no keyword overlap — partial role, downgrades to WEAK.
# ----------------------------------------------------------------------


def test_family_match_no_keyword_is_weak(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    """Same role family, but title doesn't contain any seeker keyword."""
    vac = copy.deepcopy(vacancy_direct_hire)
    vac["title"] = "Frontend React Developer"  # same family in fixture, no keyword overlap

    verdict = validator.validate(seeker_engineering, vac)
    # Family hit but no keyword -> role_alignment = PARTIAL -> can't be STRONG.
    assert verdict.verdict == FitVerdict.WEAK
    assert verdict.fit_dimensions["role_alignment"] == FitDimension.PARTIAL


# ----------------------------------------------------------------------
# Seniority out of range — downgrades to WEAK.
# ----------------------------------------------------------------------


def test_seniority_miss_downgrades_to_weak(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    """Junior vacancy for a senior+lead seeker is two ladder rungs away -> MISS -> WEAK."""
    vac = copy.deepcopy(vacancy_direct_hire)
    vac["seniority_level"] = "junior"

    verdict = validator.validate(seeker_engineering, vac)
    assert verdict.verdict == FitVerdict.WEAK
    assert verdict.fit_dimensions["seniority_fit"] == FitDimension.MISS


def test_seniority_one_rung_off_is_partial(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    """Mid-level vacancy for senior+lead seeker is one rung off -> PARTIAL."""
    vac = copy.deepcopy(vacancy_direct_hire)
    vac["seniority_level"] = "mid"

    verdict = validator.validate(seeker_engineering, vac)
    assert verdict.fit_dimensions["seniority_fit"] == FitDimension.PARTIAL


# ----------------------------------------------------------------------
# Low signal — short description triggers the flag but never blocks a verdict.
# ----------------------------------------------------------------------


def test_short_description_flags_low_signal(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    vac = copy.deepcopy(vacancy_direct_hire)
    vac["description"] = "Short blurb."  # well under 800 chars

    verdict = validator.validate(seeker_engineering, vac)
    assert verdict.low_signal is True
    # The verdict itself is still emitted — we never refuse to classify.
    assert verdict.verdict in {FitVerdict.STRONG, FitVerdict.WEAK, FitVerdict.NO_FIT}


def test_long_description_clears_low_signal(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    """Existing fixture description happens to be short -> pad it and re-check."""
    vac = copy.deepcopy(vacancy_direct_hire)
    vac["description"] = "x " * 500  # 1000 chars

    verdict = validator.validate(seeker_engineering, vac)
    assert verdict.low_signal is False


# ----------------------------------------------------------------------
# Country gating — excluded countries override matching countries.
# ----------------------------------------------------------------------


def test_excluded_country_blocks_context(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    seeker = copy.deepcopy(seeker_engineering)
    # Exclude every country the vacancy posts in.
    seeker["geography"]["countries_excluded"] = ["CH", "DE", "AT"]

    verdict = validator.validate(seeker, vacancy_direct_hire)
    # All-three context check fails on country -> at most 2 of 3 -> not MATCH.
    assert verdict.fit_dimensions["context_fit"] != FitDimension.MATCH


def test_global_scope_accepts_any_country(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    seeker = copy.deepcopy(seeker_engineering)
    seeker["geography"]["countries"] = []  # would normally block
    seeker["geography"]["scope"] = "global"

    verdict = validator.validate(seeker, vacancy_direct_hire)
    # global scope means country alone shouldn't break context.
    # (language + employment may still gate, but country itself passes.)
    assert verdict.verdict == FitVerdict.STRONG


# ----------------------------------------------------------------------
# Verdict serialisation — round-trips cleanly.
# ----------------------------------------------------------------------


def test_verdict_to_dict_round_trip(
    validator: RuleBasedValidator,
    seeker_engineering: dict,
    vacancy_direct_hire: dict,
) -> None:
    verdict = validator.validate(seeker_engineering, vacancy_direct_hire)
    payload = verdict.to_dict()
    # Must be JSON-serialisable (no enum leaks).
    json.dumps(payload)
    assert payload["verdict"] in {"strong_fit", "weak_fit", "no_fit"}
    assert set(payload["fit_dimensions"].keys()) == {
        "role_alignment",
        "seniority_fit",
        "skill_overlap",
        "context_fit",
    }


# ----------------------------------------------------------------------
# Reason sanitisation — defensive against model-injection.
# ----------------------------------------------------------------------


def test_reason_strips_html_and_truncates() -> None:
    from handshake_validator.verdict import _sanitize_reason

    raw = "<script>alert(1)</script>" + ("x" * 500)
    cleaned = _sanitize_reason(raw)
    assert "<" not in cleaned and ">" not in cleaned
    assert len(cleaned) <= 200


def test_reason_collapses_whitespace() -> None:
    from handshake_validator.verdict import _sanitize_reason

    raw = "line one\n\nline\ttwo   with    spaces"
    cleaned = _sanitize_reason(raw)
    assert "\n" not in cleaned and "\t" not in cleaned
    assert "  " not in cleaned
