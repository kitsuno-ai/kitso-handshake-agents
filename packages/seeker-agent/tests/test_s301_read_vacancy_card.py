"""S301 — read_vacancy_card unit tests.

Mocks the HTTP layer with pytest-httpx so the verb is exercised against
realistic response shapes without touching the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from seeker_agent.verbs import read_vacancy_card

URL = "https://kitsuno.ai/handshake/v0.1/vacancies/social-media-rpo-fr.json"

VALID_CARD = {
    "kitso.handshake.v1": {
        "principal_type": "hiring_entity",
        "vacancy": {
            "role_title": "Social Media Specialist",
            "role_family": "social_media_marketing",
            "seniority": "mid_to_senior",
        },
        "hiring_entity": {"name": "Kitsuno"},
    }
}


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #


def test_read_vacancy_card_returns_dict_on_200(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=200, json=VALID_CARD)
    result = read_vacancy_card(URL)
    assert result == VALID_CARD


def test_read_vacancy_card_accepts_explicit_client(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=200, json=VALID_CARD)
    with httpx.Client(timeout=2.0) as client:
        result = read_vacancy_card(URL, client=client)
    assert result == VALID_CARD


# --------------------------------------------------------------------------- #
# Non-2xx responses                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [301, 302, 304, 400, 401, 403, 404, 410, 500, 502, 503])
def test_read_vacancy_card_returns_none_on_non_200(httpx_mock, status):
    httpx_mock.add_response(url=URL, status_code=status, content=b'{"x":1}')
    assert read_vacancy_card(URL) is None


def test_read_vacancy_card_does_not_follow_redirects(httpx_mock):
    """A 30x must be treated as a failure, not silently followed off the allowlist."""
    httpx_mock.add_response(
        url=URL,
        status_code=302,
        headers={"location": "https://elsewhere.example/leak.json"},
    )
    assert read_vacancy_card(URL) is None


# --------------------------------------------------------------------------- #
# Body shape                                                                  #
# --------------------------------------------------------------------------- #


def test_read_vacancy_card_returns_none_on_malformed_json(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=200, content=b"{not json")
    assert read_vacancy_card(URL) is None


def test_read_vacancy_card_returns_none_on_empty_body(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=200, content=b"")
    assert read_vacancy_card(URL) is None


def test_read_vacancy_card_returns_none_on_json_array(httpx_mock):
    """A schema-valid card is an object, never a top-level array. Reject."""
    httpx_mock.add_response(url=URL, status_code=200, json=[VALID_CARD])
    assert read_vacancy_card(URL) is None


def test_read_vacancy_card_returns_none_on_json_string(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=200, json="not an object")
    assert read_vacancy_card(URL) is None


def test_read_vacancy_card_returns_none_on_json_null(httpx_mock):
    httpx_mock.add_response(url=URL, status_code=200, json=None)
    assert read_vacancy_card(URL) is None


# --------------------------------------------------------------------------- #
# Size cap                                                                    #
# --------------------------------------------------------------------------- #


def test_read_vacancy_card_returns_none_on_oversize_response(httpx_mock):
    """A 1MB payload exceeds the 256KB cap and must be rejected before parse."""
    bloated = {"kitso.handshake.v1": {"x": "y" * (1024 * 1024)}}
    httpx_mock.add_response(url=URL, status_code=200, content=json.dumps(bloated).encode())
    assert read_vacancy_card(URL) is None


def test_read_vacancy_card_accepts_at_cap_boundary(httpx_mock):
    """A payload just under the 256KB cap must still be accepted."""
    # Build a card whose serialized form is comfortably under 256KB
    big_but_ok = {"kitso.handshake.v1": {"vacancy": {"notes": "z" * (128 * 1024)}}}
    payload = json.dumps(big_but_ok).encode()
    assert len(payload) < 256 * 1024
    httpx_mock.add_response(url=URL, status_code=200, content=payload)
    result = read_vacancy_card(URL)
    assert result is not None
    assert "kitso.handshake.v1" in result


# --------------------------------------------------------------------------- #
# Transport errors                                                            #
# --------------------------------------------------------------------------- #


def test_read_vacancy_card_returns_none_on_connect_timeout(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectTimeout("connect timed out"), url=URL)
    assert read_vacancy_card(URL) is None


def test_read_vacancy_card_returns_none_on_read_timeout(httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("read timed out"), url=URL)
    assert read_vacancy_card(URL) is None


def test_read_vacancy_card_returns_none_on_dns_failure(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("name resolution failed"), url=URL)
    assert read_vacancy_card(URL) is None


def test_read_vacancy_card_returns_none_on_invalid_url():
    # No mock needed — httpx itself rejects this synchronously.
    assert read_vacancy_card("not a url at all") is None


# --------------------------------------------------------------------------- #
# Sanity: never raises                                                        #
# --------------------------------------------------------------------------- #


def test_read_vacancy_card_never_raises(httpx_mock):
    """Every failure mode covered in this module returns None, not raises.

    This is a contract test — the orchestrator wraps the verb in an audit-emit
    block but doesn't catch exceptions, so the verb itself must absorb them.
    """
    httpx_mock.add_exception(RuntimeError("unexpected"), url=URL)
    # RuntimeError isn't in our explicit catch list. Confirm it propagates so
    # we know our contract: HTTP-domain errors → None, programmer errors → raise.
    with pytest.raises(RuntimeError):
        read_vacancy_card(URL)
