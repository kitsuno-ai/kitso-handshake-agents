"""Compute state_hash for a sample card.

Run from this directory:
    python compute_hash.py
"""
import json
from pathlib import Path

from kitso_state_hash import seeker_state_hash, vacancy_state_hash

HERE = Path(__file__).parent
FIXTURE_ROOT = HERE.parent.parent.parent / "test-fixtures" / "v0.2"

vac = json.loads((FIXTURE_ROOT / "vacancy-card-direct-hire.json").read_text())
see = json.loads((FIXTURE_ROOT / "seeker-card-engineering.json").read_text())

print(f"vacancy {vac['slug']}: state_hash = {vacancy_state_hash(vac)}")
print(f"seeker  {see['slug']}: state_hash = {seeker_state_hash(see)}")
