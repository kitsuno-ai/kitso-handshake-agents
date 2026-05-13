"""S301 — CloudflareProvider + FailoverProvider tests."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
import pytest

from seeker_agent.classifier import (
    Classification,
    ClassifierSchemaError,
    PostRecord,
)
from seeker_agent.providers.cloudflare import CloudflareError, CloudflareProvider
from seeker_agent.providers.failover import FailoverExhausted, FailoverProvider
from seeker_agent.providers.mistral import MistralError


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sample_post() -> PostRecord:
    return PostRecord(
        venue="gonzo_bluesky",
        post_id="cf-test-1",
        post_text="Senior Backend Engineer (Go). Berlin, Germany. Remote-friendly.",
        observed_at="2026-05-13T12:00:00+00:00",
        post_title="Hiring Senior Backend Engineer",
        submolt_or_channel="gonzo_bluesky",
        language_hint="en",
    )


def _valid_classification_body() -> dict[str, Any]:
    """A schema-valid JSON object the model would return."""
    return {
        "is_job_shaped": True,
        "relevance": 0.95,
        "extracted_role_title": "Senior Backend Engineer (Go)",
        "extracted_role_family": "software_engineering",
        "extracted_seniority": "senior",
        "extracted_company": None,
        "extracted_geography": {"country_hint": "DE", "remote_hint": "fully_remote"},
        "has_vacancy_card_url": False,
        "vacancy_card_url": None,
        "spam_signals": [],
        "language_detected": "en",
        "reasoning": "EU technical role.",
        "model": "x",
        "prompt_version": "x",
    }


def _cf_response_envelope(content: dict[str, Any]) -> dict[str, Any]:
    """Wrap a content dict in CF Workers AI's standard response envelope."""
    return {
        "result": {"response": json.dumps(content)},
        "success": True,
        "errors": [],
        "messages": [],
    }


CF_URL_RE = re.compile(
    r"^https://api\.cloudflare\.com/client/v4/accounts/[^/]+/ai/run/.+$"
)


# =========================================================================== #
# CloudflareProvider                                                          #
# =========================================================================== #


class TestCloudflareProvider:

    def test_classify_ok_returns_classification(self, httpx_mock, sample_post):
        body = _valid_classification_body()
        httpx_mock.add_response(url=CF_URL_RE, json=_cf_response_envelope(body))
        provider = CloudflareProvider(
            api_token="tok-x", account_id="acct-y", min_gap_seconds=0.0
        )
        result = provider.classify(sample_post)
        assert isinstance(result, Classification)
        assert result.is_job_shaped is True
        assert result.relevance == 0.95
        assert result.model == provider._model
        assert result.prompt_version == "seeker-classifier-v0.3"

    def test_classify_requires_credentials(self):
        with pytest.raises(ValueError, match="api_token is required"):
            CloudflareProvider(api_token="", account_id="acct")
        with pytest.raises(ValueError, match="account_id is required"):
            CloudflareProvider(api_token="tok", account_id="")

    def test_classify_raises_on_non_200(self, httpx_mock, sample_post):
        httpx_mock.add_response(url=CF_URL_RE, status_code=500, content=b"server boom")
        provider = CloudflareProvider(
            api_token="tok", account_id="acct", min_gap_seconds=0.0
        )
        with pytest.raises(CloudflareError, match="500"):
            provider.classify(sample_post)

    def test_classify_raises_on_success_false(self, httpx_mock, sample_post):
        body = {
            "result": None,
            "success": False,
            "errors": [{"code": 7000, "message": "no route"}],
            "messages": [],
        }
        httpx_mock.add_response(url=CF_URL_RE, json=body)
        provider = CloudflareProvider(
            api_token="tok", account_id="acct", min_gap_seconds=0.0
        )
        with pytest.raises(CloudflareError, match="success=false"):
            provider.classify(sample_post)

    def test_classify_retries_on_429(self, httpx_mock, sample_post, monkeypatch):
        """Two 429s then a 200 should succeed without raising."""
        # Avoid actually sleeping during retry backoff
        monkeypatch.setattr("seeker_agent.providers.cloudflare.time.sleep", lambda _: None)
        httpx_mock.add_response(url=CF_URL_RE, status_code=429, headers={"retry-after": "0.1"})
        httpx_mock.add_response(url=CF_URL_RE, status_code=429, headers={"retry-after": "0.1"})
        body = _valid_classification_body()
        httpx_mock.add_response(url=CF_URL_RE, json=_cf_response_envelope(body))

        provider = CloudflareProvider(
            api_token="tok", account_id="acct", min_gap_seconds=0.0
        )
        result = provider.classify(sample_post)
        assert result.is_job_shaped is True

    def test_classify_raises_on_malformed_json_response(self, httpx_mock, sample_post):
        body = {
            "result": {"response": "{not json at all"},
            "success": True,
            "errors": [],
        }
        httpx_mock.add_response(url=CF_URL_RE, json=body)
        provider = CloudflareProvider(
            api_token="tok", account_id="acct", min_gap_seconds=0.0
        )
        with pytest.raises(ClassifierSchemaError, match="not valid JSON"):
            provider.classify(sample_post)

    def test_classify_handles_choices_envelope(self, httpx_mock, sample_post):
        """Some CF models return a choices[] structure (OpenAI-style)."""
        body = {
            "result": {
                "choices": [
                    {"message": {"content": json.dumps(_valid_classification_body())}}
                ]
            },
            "success": True,
            "errors": [],
        }
        httpx_mock.add_response(url=CF_URL_RE, json=body)
        provider = CloudflareProvider(
            api_token="tok", account_id="acct", min_gap_seconds=0.0
        )
        result = provider.classify(sample_post)
        assert result.is_job_shaped is True

    def test_classify_strips_markdown_fences(self, httpx_mock, sample_post):
        body = _valid_classification_body()
        # Simulate a model that emits ```json ... ``` despite JSON mode
        wrapped = f"```json\n{json.dumps(body)}\n```"
        envelope = {
            "result": {"response": wrapped},
            "success": True,
            "errors": [],
        }
        httpx_mock.add_response(url=CF_URL_RE, json=envelope)
        provider = CloudflareProvider(
            api_token="tok", account_id="acct", min_gap_seconds=0.0
        )
        result = provider.classify(sample_post)
        assert result.is_job_shaped is True


# =========================================================================== #
# FailoverProvider                                                            #
# =========================================================================== #


class _FakeProvider:
    """Test double — records call count and replays a programmed result list."""

    def __init__(self, name: str, results: list) -> None:
        self.name = name
        self._results = list(results)
        self.calls = 0

    def classify(self, post: PostRecord) -> Classification:
        self.calls += 1
        if not self._results:
            raise RuntimeError(f"{self.name} ran out of results")
        r = self._results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def _ok_classification() -> Classification:
    return Classification.from_dict(_valid_classification_body())


class TestFailoverProvider:

    def test_init_rejects_empty(self):
        with pytest.raises(ValueError, match="at least one provider"):
            FailoverProvider([])

    def test_primary_success_does_not_touch_fallback(self, sample_post):
        primary = _FakeProvider("primary", [_ok_classification()])
        fallback = _FakeProvider("fallback", [_ok_classification()])
        chain = FailoverProvider([primary, fallback])
        result = chain.classify(sample_post)
        assert result.is_job_shaped is True
        assert primary.calls == 1
        assert fallback.calls == 0

    def test_primary_provider_error_promotes_fallback(self, sample_post):
        primary = _FakeProvider("primary", [MistralError("Mistral returned 429: ...")])
        fallback = _FakeProvider("fallback", [_ok_classification()])
        chain = FailoverProvider([primary, fallback], cooldown_seconds=60)
        result = chain.classify(sample_post)
        assert result.is_job_shaped is True
        assert primary.calls == 1
        assert fallback.calls == 1
        # Primary should now be marked unhealthy
        assert "primary" in chain._cooldown_until

    def test_cooldown_routes_around_unhealthy_primary(self, sample_post):
        """After the first failure, subsequent calls skip the primary entirely."""
        primary = _FakeProvider(
            "primary",
            [MistralError("rate limited"), _ok_classification()],
        )
        fallback = _FakeProvider(
            "fallback",
            [_ok_classification(), _ok_classification()],
        )
        chain = FailoverProvider([primary, fallback], cooldown_seconds=60)
        chain.classify(sample_post)
        # Second call: primary should be skipped (still in cooldown)
        chain.classify(sample_post)
        assert primary.calls == 1
        assert fallback.calls == 2

    def test_cooldown_expires_and_primary_retried(self, sample_post, monkeypatch):
        primary = _FakeProvider(
            "primary",
            [MistralError("rate limited"), _ok_classification()],
        )
        fallback = _FakeProvider("fallback", [_ok_classification()])
        # tiny cooldown so we can step past it
        chain = FailoverProvider([primary, fallback], cooldown_seconds=0.001)
        chain.classify(sample_post)
        time.sleep(0.01)
        result = chain.classify(sample_post)
        assert result.is_job_shaped is True
        assert primary.calls == 2  # primary retried after cooldown
        assert fallback.calls == 1

    def test_all_providers_unhealthy_raises_failover_exhausted(self, sample_post):
        primary = _FakeProvider("primary", [MistralError("down")])
        fallback = _FakeProvider("fallback", [CloudflareError("also down")])
        chain = FailoverProvider([primary, fallback])
        with pytest.raises(FailoverExhausted):
            chain.classify(sample_post)
        assert primary.calls == 1
        assert fallback.calls == 1

    def test_schema_error_advances_without_marking_unhealthy(self, sample_post):
        """ClassifierSchemaError is a per-call hiccup, not unhealthiness."""
        primary = _FakeProvider("primary", [ClassifierSchemaError("bad json"), _ok_classification()])
        fallback = _FakeProvider("fallback", [_ok_classification()])
        chain = FailoverProvider([primary, fallback], cooldown_seconds=60)
        chain.classify(sample_post)
        # Primary should NOT be cooled down
        assert "primary" not in chain._cooldown_until
        # Fallback handled this call
        assert primary.calls == 1
        assert fallback.calls == 1

    def test_programmer_error_propagates(self, sample_post):
        """A bare RuntimeError or ValueError is not a provider-availability issue."""
        primary = _FakeProvider("primary", [ValueError("bug in caller")])
        fallback = _FakeProvider("fallback", [_ok_classification()])
        chain = FailoverProvider([primary, fallback])
        with pytest.raises(ValueError, match="bug in caller"):
            chain.classify(sample_post)
        # Fallback was NOT consulted
        assert fallback.calls == 0

    def test_network_error_marks_unhealthy(self, sample_post):
        primary = _FakeProvider("primary", [httpx.ConnectError("dns failed")])
        fallback = _FakeProvider("fallback", [_ok_classification()])
        chain = FailoverProvider([primary, fallback])
        result = chain.classify(sample_post)
        assert result.is_job_shaped is True
        assert "primary" in chain._cooldown_until

    def test_success_clears_cooldown(self, sample_post, monkeypatch):
        """A subsequent success on a previously-failed provider clears cooldown."""
        primary = _FakeProvider(
            "primary",
            [MistralError("down"), _ok_classification()],
        )
        fallback = _FakeProvider(
            "fallback",
            [_ok_classification()],
        )
        chain = FailoverProvider([primary, fallback], cooldown_seconds=0.001)
        chain.classify(sample_post)
        assert "primary" in chain._cooldown_until
        time.sleep(0.01)
        chain.classify(sample_post)
        # primary succeeded, cooldown cleared
        assert "primary" not in chain._cooldown_until

    def test_name_is_chain_join(self):
        chain = FailoverProvider(
            [_FakeProvider("a", []), _FakeProvider("b", [])]
        )
        assert chain.name == "a+b"
