"""Minimal demonstration of v0.2 policy evaluation.

Run with::

    python examples/basic.py

"""

from __future__ import annotations

from kitso_policy_match import evaluate, FIRE_L1


VACANCY_CARD = {
    "vacancy_card_id": "0193e8ac-3f21-7a90-bef0-1c8f4a5d2e91",
    "title": "Senior Python Engineer",
    # § Policy: what the vacancy requires of seekers
    "policy": {
        "criteria": [
            {"field": "languages", "operator": "any",
             "values": ["python"], "gate": "hard"},
            {"field": "salary_min", "operator": "lte",
             "value": 100000, "gate": "soft"},
            {"field": "work_rights", "operator": "in",
             "values": ["EU", "CH"], "gate": "hard"},
        ]
    },
    # § Traits: what the vacancy offers
    "traits": {
        "salary_min": 90000,
        "languages": ["python", "kubernetes"],
        "work_arrangement": "remote",
    },
}

SEEKER_CARD = {
    "seeker_card_id": "0193e8b0-1c8f-4a5d-2e91-3f217a90bef0",
    "policy": {
        "criteria": [
            {"field": "work_arrangement", "operator": "in",
             "values": ["remote", "hybrid"], "gate": "hard"},
        ]
    },
    "traits": {
        "languages": ["python", "go", "rust"],
        "salary_min": 85000,
        "work_rights": "EU",
    },
}


def main() -> None:
    result = evaluate(
        card_policy=VACANCY_CARD["policy"],
        card_traits=VACANCY_CARD["traits"],
        seeker_policy=SEEKER_CARD["policy"],
        seeker_traits=SEEKER_CARD["traits"],
        stage="L1",
    )

    print(f"Outcome:  {result['outcome']}")
    print(f"Stage:    {result['stage']}")
    print(f"Matched:  {len(result['matched_criteria'])}")
    print(f"Blocking: {len(result['blocking_criteria'])}")

    if result["outcome"] == FIRE_L1:
        print("\n  Safe to fire L1 against this vacancy.")
    else:
        print("\n  L1 blocked. Blocking criteria:")
        for c in result["blocking_criteria"]:
            print(f"   - {c['field']} ({c['operator']}, gate={c['gate']}): {c.get('reason', c['outcome'])}")


if __name__ == "__main__":
    main()
