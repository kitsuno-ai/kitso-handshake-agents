#!/usr/bin/env python3
"""S336-S1c — policy_match smoke runner against the live staging seeker card.

Usage:
  python3 smoke_live.py [seeker_slug]

Default seeker slug: sk_8dd118c469c4 (Greg's L&D profile on staging).

Runs three scenarios:
  A. Clean DE/CH vacancy with no extra policy criteria → expect FIRE_L1
  B. RU-only vacancy → expect BLOCK_L1 (seeker's countries_excluded fires)
  C. The existing vacancy-card-direct-hire fixture + its handshake-policy
     companion → expect BLOCK_L1 (documents a known field-collision in the
     fixture: `languages: any: [python]` requires programming languages in
     the spoken-languages trait — surfaces the v0.2 fixture gap to fix later)

Exit code is 0 if all scenarios match expected outcomes, 1 otherwise.

Spec ref: /opt/sf4l-staging/docs/s336-handshake-pipeline-integration-spec.md §5 Session 1
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

# Project root so we can import the local policy_match package
THIS = Path(__file__).resolve()
SRC = THIS.parent.parent / "src"
sys.path.insert(0, str(SRC))

from kitso_policy_match import policy_match  # noqa: E402

STAGING = "https://staging.kitsuno.ai"
FIXTURES = THIS.parent.parent.parent.parent / "test-fixtures" / "v0.2"


def fetch_seeker(slug: str) -> dict:
    url = f"{STAGING}/handshake/v0.2/seeker-cards/{slug}.json"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def colored(outcome: str) -> str:
    g, r, y, e = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
    if outcome.startswith("FIRE") or outcome.startswith("ELIGIBLE"):
        return f"{g}{outcome}{e}"
    if outcome.startswith("BLOCK"):
        return f"{r}{outcome}{e}"
    return f"{y}{outcome}{e}"


def print_scenario(name: str, expected: str, result: dict) -> bool:
    actual = result["outcome"]
    ok = actual == expected
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f"\n{mark} {name}")
    print(f"  expected: {colored(expected)}    actual: {colored(actual)}    stage: {result['stage']}")
    matched = result.get("matched_criteria") or []
    blocking = result.get("blocking_criteria") or []
    print(f"  criteria evaluated: {len(matched)}    blocking: {len(blocking)}")
    for c in matched:
        side = c.get("side", "?")
        outcome = c["outcome"]
        gate = c["gate"]
        field = c["field"]
        op = c["operator"]
        reason = c.get("reason") or ""
        marker = "✓" if outcome == "match" else ("⨯" if outcome == "mismatch" else "?")
        print(f"    {marker} [{side:18s}] {field:20s} {op:8s} ({gate:6s}) → {outcome}  {reason}")
    return ok


def main(seeker_slug: str = "sk_8dd118c469c4") -> int:
    print(f"Fetching seeker: {STAGING}/handshake/v0.2/seeker-cards/{seeker_slug}.json")
    seeker = fetch_seeker(seeker_slug)
    seeker_traits = seeker.get("seeker_traits") or {}
    seeker_policy = seeker.get("seeker_policy") or {"criteria": []}

    print(f"  status: {seeker.get('status')}")
    print(f"  countries: {len(seeker_traits.get('geography', {}).get('countries', []))}")
    print(f"  countries_excluded: {seeker_traits.get('geography', {}).get('countries_excluded', [])}")
    print(f"  seeker_policy.criteria: {len(seeker_policy.get('criteria', []))}")

    all_ok = True

    # ── Scenario A: clean DE/CH vacancy ────────────────────────────
    vacancy_clean_traits = {
        "title": "Senior Learning Engineer",
        "role_family": "learning_engineering",
        "seniority_level": "senior",
        "country_codes": ["DE", "CH"],
        "skills": ["learning design", "edtech", "ai transformation"],
    }
    res_a = policy_match.evaluate(
        card_policy={"criteria": []},
        card_traits=vacancy_clean_traits,
        seeker_policy=seeker_policy,
        seeker_traits=seeker_traits,
        stage="L1",
    )
    all_ok &= print_scenario("Scenario A — clean DE/CH vacancy, no extra criteria", "FIRE_L1", res_a)

    # ── Scenario B: RU-only vacancy ─────────────────────────────────
    vacancy_ru_traits = {
        "title": "AI Strategist",
        "role_family": "ai_strategy",
        "seniority_level": "senior",
        "country_codes": ["RU"],
    }
    res_b = policy_match.evaluate(
        card_policy={"criteria": []},
        card_traits=vacancy_ru_traits,
        seeker_policy=seeker_policy,
        seeker_traits=seeker_traits,
        stage="L1",
    )
    all_ok &= print_scenario("Scenario B — RU-only vacancy, seeker excludes RU", "BLOCK_L1", res_b)

    # ── Scenario C: existing vacancy fixture + its companion policy ─
    try:
        vacancy_fix = json.loads((FIXTURES / "vacancy-card-direct-hire.json").read_text())
        policy_fix = json.loads((FIXTURES / "handshake-policy-direct-hire.json").read_text())
    except FileNotFoundError as e:
        print(f"\n\033[33m⚠ Scenario C skipped — fixture not found: {e}\033[0m")
    else:
        res_c = policy_match.evaluate(
            card_policy=policy_fix,
            card_traits=vacancy_fix,
            seeker_policy=seeker_policy,
            seeker_traits=seeker_traits,
            stage="L1",
        )
        # Expected BLOCK_L1: the fixture's `languages: any: [python]` criterion
        # collides with the spoken-language shape of the seeker's languages
        # field. Surfaces a known v0.2 fixture gap.
        all_ok &= print_scenario(
            "Scenario C — vacancy fixture vs. live seeker (known field-collision)",
            "BLOCK_L1", res_c,
        )

    print()
    print("=" * 70)
    if all_ok:
        print("\033[32m✓ All scenarios matched expected outcomes.\033[0m")
        return 0
    print("\033[31m✗ Some scenarios diverged from expected.\033[0m")
    return 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
