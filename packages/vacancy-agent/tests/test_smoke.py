"""Smoke tests for the Vacancy Agent.

These tests do NOT call the venue API. They verify:
- A valid card validates
- An invalid card fails validation
- The post body extracts the right fields
- The CLI refuses to run live without credentials
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vacancy_agent.cards import extract_post_summary, validate_card
from vacancy_agent.config import Settings
from vacancy_agent.main import _format_post_body, _slug_from_card_path

REPO_ROOT = Path(__file__).resolve().parents[3]
VALID_FIXTURES = REPO_ROOT / "test-fixtures" / "valid"
INVALID_FIXTURES = REPO_ROOT / "test-fixtures" / "invalid"


def _load(path: Path) -> dict:
    with path.open() as fp:
        return json.load(fp)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "vacancy-card-direct-hire.json",
        "vacancy-card-rpo.json",
    ],
)
def test_valid_fixtures_validate(fixture_name):
    """Every fixture in test-fixtures/valid/ must validate against the v0.1 schema."""
    result = validate_card(VALID_FIXTURES / fixture_name)
    assert result.ok, f"Expected {fixture_name} to validate. Errors: {result.errors}"


def test_invalid_fixture_fails_validation():
    """An obviously-bad card should fail validation, not silently pass."""
    result = validate_card(INVALID_FIXTURES / "vacancy-card-missing-required.json")
    assert not result.ok
    assert len(result.errors) > 0


def test_missing_file_returns_error_not_raise():
    """validate_card must never raise; it returns ok=False with an explanation."""
    result = validate_card("/tmp/definitely-does-not-exist-12345.json")
    assert not result.ok
    assert any("not found" in e.lower() for e in result.errors)


def test_post_summary_extracts_fields():
    card = _load(VALID_FIXTURES / "vacancy-card-direct-hire.json")
    summary = extract_post_summary(card)
    assert summary["role_title"] == card["kitso.handshake.v1"]["vacancy"]["role_title"]
    assert summary["employment_type"] == card["kitso.handshake.v1"]["vacancy"]["employment_type"]


def test_format_post_body_includes_links():
    card = _load(VALID_FIXTURES / "vacancy-card-direct-hire.json")
    title, content = _format_post_body(
        card,
        card_url="https://example.com/handshake/v0.1/vacancies/role.json",
        jd_url="https://example.com/jobs/role",
    )
    assert len(title) <= 120
    assert "Kitso Handshake v0.1 card" in content
    assert "https://example.com/handshake/v0.1/vacancies/role.json" in content
    assert "https://example.com/jobs/role" in content


def test_settings_fails_closed_for_live_mode():
    """If MOLTBOOK_API_KEY is unset, check_live_mode must surface it."""
    settings = Settings(
        moltbook_api_key=None,
        agent_kill_token=None,
    )
    missing = settings.check_live_mode()
    assert "MOLTBOOK_API_KEY" in missing
    assert "AGENT_KILL_TOKEN" in missing


def test_slug_from_card_path():
    assert _slug_from_card_path(Path("/tmp/social-media-specialist.json")) == "social-media-specialist"
    assert _slug_from_card_path(Path("foo/bar/agentic-ai-developer.json")) == "agentic-ai-developer"
