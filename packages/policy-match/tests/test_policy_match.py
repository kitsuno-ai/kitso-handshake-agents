"""Reference tests for the v0.2 policy evaluator.

These cover the gate semantics specified in v0.2 §5.5: hard mismatches block at
both L1 and L2; soft mismatches only block at L2; informational never block.
"""

from __future__ import annotations

from kitso_policy_match import (
    evaluate,
    FIRE_L1,
    BLOCK_L1,
    BLOCK_L2,
    VALID_GATES,
)


# ── trivial structure ──────────────────────────────────────────────


def test_empty_policies_fire():
    """No criteria on either side → handshake fires."""
    result = evaluate(
        card_policy={"criteria": []},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={},
        stage="L1",
    )
    assert result["outcome"] == FIRE_L1


def test_none_policies_fire():
    """None policies (unspecified) are treated as no filters."""
    result = evaluate(None, None, None, None, stage="L1")
    assert result["outcome"] == FIRE_L1


def test_valid_gates():
    assert VALID_GATES == frozenset({"hard", "soft", "informational"})


# ── hard gate at L1 ────────────────────────────────────────────────


def test_hard_match_fires():
    result = evaluate(
        card_policy={"criteria": [
            {"field": "languages", "operator": "any",
             "values": ["python"], "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"languages": ["python", "go"]},
        stage="L1",
    )
    assert result["outcome"] == FIRE_L1


def test_hard_mismatch_blocks_l1():
    result = evaluate(
        card_policy={"criteria": [
            {"field": "languages", "operator": "any",
             "values": ["java"], "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"languages": ["python", "go"]},
        stage="L1",
    )
    assert result["outcome"] == BLOCK_L1


# ── soft gate ──────────────────────────────────────────────────────


def test_soft_mismatch_fires_at_l1():
    """Soft mismatches don't block L1."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "salary_min", "operator": "gte",
             "value": 5000, "gate": "soft"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"salary_min": 3000},
        stage="L1",
    )
    assert result["outcome"] == FIRE_L1


def test_soft_mismatch_blocks_at_l2():
    """Same soft mismatch DOES block at L2."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "salary_min", "operator": "gte",
             "value": 5000, "gate": "soft"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"salary_min": 3000},
        stage="L2",
    )
    assert result["outcome"] == BLOCK_L2


# ── symmetry ───────────────────────────────────────────────────────


def test_symmetric_seeker_filter_can_block_vacancy():
    """Seeker's criteria also check against vacancy's traits."""
    result = evaluate(
        card_policy={"criteria": []},
        card_traits={"work_arrangement": "onsite"},
        seeker_policy={"criteria": [
            {"field": "work_arrangement", "operator": "in",
             "values": ["remote", "hybrid"], "gate": "hard"}
        ]},
        seeker_traits={},
        stage="L1",
    )
    assert result["outcome"] == BLOCK_L1


# ── unknown trait ──────────────────────────────────────────────────


def test_hard_gate_unknown_blocks_at_l1():
    """v0.2 §5.5: hard-gate unknowns block at L1 (don't fire on uncertainty)."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "languages", "operator": "any",
             "values": ["python"], "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={},  # seeker has not declared languages
        stage="L1",
    )
    assert result["outcome"] == BLOCK_L1


# ── informational ──────────────────────────────────────────────────


def test_informational_never_blocks():
    """Informational gates never block."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "languages", "operator": "any",
             "values": ["fortran"], "gate": "informational"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"languages": ["python"]},
        stage="L2",
    )
    # Informational mismatch should NOT block — outcome stays in fire/eligible
    assert result["outcome"] not in (BLOCK_L1, BLOCK_L2)


# ── present / absent ───────────────────────────────────────────────


def test_present_operator():
    """present passes when the trait is declared, fails when empty."""
    has = evaluate(
        card_policy={"criteria": [
            {"field": "work_rights", "operator": "present", "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"work_rights": "EU"},
        stage="L1",
    )
    assert has["outcome"] == FIRE_L1

    lacks = evaluate(
        card_policy={"criteria": [
            {"field": "work_rights", "operator": "present", "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={},
        stage="L1",
    )
    assert lacks["outcome"] == BLOCK_L1


# ── result shape ───────────────────────────────────────────────────


def test_result_shape():
    result = evaluate(
        card_policy={"criteria": []},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={},
        stage="L1",
    )
    assert set(result.keys()) >= {"outcome", "stage", "matched_criteria", "blocking_criteria"}
    assert result["stage"] == "L1"


# ─ v0.2.1 additions ─────────────────────────────────────────────────────────

def test_any_default_min_matches_is_one():
    """`any` operator without explicit min_matches matches with single overlap."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "skills", "operator": "any",
             "values": ["python", "go", "rust"], "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"skills": ["python", "javascript"]},
        stage="L1",
    )
    assert result["outcome"] == "FIRE_L1", result


def test_any_min_matches_two_succeeds_when_overlap_two():
    """`min_matches: 2` succeeds when seeker has 2+ of the listed skills."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "skills", "operator": "any",
             "values": ["python", "kubernetes", "postgres"],
             "min_matches": 2, "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"skills": ["python", "kubernetes", "rust"]},
        stage="L1",
    )
    assert result["outcome"] == "FIRE_L1", result


def test_any_min_matches_two_blocks_when_overlap_one():
    """`min_matches: 2` blocks when seeker only has 1 of the listed skills."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "skills", "operator": "any",
             "values": ["python", "kubernetes", "postgres"],
             "min_matches": 2, "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"skills": ["python", "rust"]},
        stage="L1",
    )
    assert result["outcome"] == "BLOCK_L1", result
    assert result["blocking_criteria"], "should record what blocked"
    reason = result["blocking_criteria"][0].get("reason", "")
    assert "1 of required 2" in reason or "only 1" in reason, reason


def test_split_trait_languages_unwraps_works_in():
    """Languages stored as {speaks, works_in} split shape: criterion against
    `languages` is evaluated against the `works_in` inner list."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "languages", "operator": "any",
             "values": ["en", "de"], "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"languages": {
            "speaks": [{"language": "fr", "level": "Native"},
                       {"language": "en", "level": "C2"}],
            "works_in": ["en", "fr"],
        }},
        stage="L1",
    )
    # works_in = [en, fr]; criterion accepts [en, de] — en overlaps → match
    assert result["outcome"] == "FIRE_L1", result


def test_split_trait_languages_legacy_list_still_works():
    """Legacy languages shape ([{language, level}, ...] or [code, ...]) must
    continue to evaluate correctly — pass-through path."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "languages", "operator": "any",
             "values": ["en"], "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"languages": ["en", "de"]},
        stage="L1",
    )
    assert result["outcome"] == "FIRE_L1", result


def test_split_trait_languages_works_in_misses():
    """If works_in does not overlap with criterion, BLOCK as expected."""
    result = evaluate(
        card_policy={"criteria": [
            {"field": "languages", "operator": "any",
             "values": ["de"], "gate": "hard"}
        ]},
        card_traits={},
        seeker_policy={"criteria": []},
        seeker_traits={"languages": {
            "speaks": [{"language": "de", "level": "Native"}],
            "works_in": ["en"],  # speaks German but only works in English
        }},
        stage="L1",
    )
    assert result["outcome"] == "BLOCK_L1", result

