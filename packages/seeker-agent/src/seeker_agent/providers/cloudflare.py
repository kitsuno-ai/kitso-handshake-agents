"""Cloudflare Workers AI classifier provider.

The free tier of Workers AI hosts open models (Llama, Qwen, etc.) and gives
agentic workloads a fallback when the primary (Mistral) is rate-limited.
This provider implements the same :class:`ClassifierProvider` protocol so it
slots into :class:`FailoverProvider` without the orchestrator needing to know
which provider answered.

Endpoint shape (POST):

    https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}

Response shape on 200:

    {"result": {"response": "<JSON text from the model>"}, "success": true}

Failures appear as ``success: false`` even with a 200 status, plus the usual
4xx/5xx HTTP codes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx

from ..classifier import Classification, ClassifierSchemaError, PostRecord, validate_payload
from .mistral import _build_system_message, _build_user_message, _load_prompt_template

log = logging.getLogger(__name__)


class CloudflareError(RuntimeError):
    """Raised when Cloudflare returns an error status or success=false body."""


class CloudflareProvider:
    """Cloudflare Workers AI classifier — failover provider for Mistral.

    The pacing, retry, and JSON-mode parsing mirror :class:`MistralProvider`
    closely so the failover wrapper sees a stable contract.

    Pacing: one request per ``min_gap_seconds`` (default 1.0). Thread-safe.
    Tests inject a custom :class:`httpx.Client` via ``client=``.
    """

    name = "cloudflare"

    def __init__(
        self,
        api_token: str,
        account_id: str,
        model: str = "@cf/qwen/qwen1.5-14b-chat-awq",
        api_base: str = "https://api.cloudflare.com/client/v4",
        prompt_version: str = "seeker-classifier-v0.3",
        temperature: float = 0.1,
        timeout_seconds: float = 30.0,
        max_post_chars: int = 8000,
        min_gap_seconds: float = 1.0,
        client: httpx.Client | None = None,
        prompt_template: str | None = None,
    ) -> None:
        if not api_token:
            raise ValueError("api_token is required (set CLOUDFLARE_API_TOKEN)")
        if not account_id:
            raise ValueError("account_id is required (set CLOUDFLARE_ACCOUNT_ID)")
        self._api_token = api_token
        self._account_id = account_id
        self._model = model
        self._api_base = api_base.rstrip("/")
        self._prompt_version = prompt_version
        self._temperature = temperature
        self._timeout = timeout_seconds
        self._max_post_chars = max_post_chars
        self._min_gap = min_gap_seconds
        self._client = client
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._last_call_at: float = 0.0
        self._system_prompt = _build_system_message(
            prompt_template or _load_prompt_template()
        )

    # --- Pacing ----------------------------------------------------------- #

    def _wait_for_slot(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call_at
            if self._last_call_at > 0 and elapsed < self._min_gap:
                time.sleep(self._min_gap - elapsed)
            self._last_call_at = time.monotonic()

    # --- Main call -------------------------------------------------------- #

    def classify(self, post: PostRecord) -> Classification:
        self._wait_for_slot()

        payload = {
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": _build_user_message(post, self._max_post_chars)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self._temperature,
        }
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
            "User-Agent": "kitso-handshake-seeker-agent/0.2.0",
        }
        url = f"{self._api_base}/accounts/{self._account_id}/ai/run/{self._model}"

        client = self._client or httpx.Client(timeout=self._timeout)
        max_retries = 3
        attempt = 0
        try:
            while True:
                resp = client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    break
                if resp.status_code == 429 and attempt < max_retries:
                    retry_after_hdr = resp.headers.get("retry-after", "")
                    try:
                        delay = float(retry_after_hdr)
                    except (TypeError, ValueError):
                        delay = 1.0
                    delay = max(delay, 1.0) + 0.5
                    log.info(
                        "Cloudflare 429 (attempt %d/%d); sleeping %.1fs",
                        attempt + 1, max_retries, delay,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise CloudflareError(
                    f"Cloudflare returned {resp.status_code}: {resp.text[:200]}"
                )
        finally:
            if self._owns_client:
                client.close()

        body = resp.json()
        # CF wraps every response in {"success": bool, "errors": [...], "result": {...}}.
        # A 200 with success=false is still a failure — surface it.
        if not body.get("success"):
            errs = body.get("errors") or []
            raise CloudflareError(
                f"Cloudflare success=false: {errs[:2] if errs else 'no error detail'}"
            )

        content = self._extract_content(body)
        content = content.strip()
        # Defensive: strip markdown fences if the model emits them despite JSON mode.
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ClassifierSchemaError(
                f"Cloudflare response was not valid JSON: {exc}; head={content[:200]!r}"
            ) from exc

        # Force model + prompt_version to match what we actually used.
        parsed["model"] = self._model
        parsed["prompt_version"] = self._prompt_version

        validated = validate_payload(parsed)
        return Classification.from_dict(validated)

    # --- Helpers ---------------------------------------------------------- #

    @staticmethod
    def _extract_content(body: dict[str, Any]) -> str:
        """Pull the model's text response out of CF's envelope.

        CF returns ``{"result": {"response": "<text>"}}`` for text-generation
        models. Some models also return ``{"result": "<text>"}`` directly.
        Handle both shapes.
        """
        result = body.get("result")
        if isinstance(result, dict):
            response = result.get("response")
            if isinstance(response, str):
                return response
            # Some routes return choices[] like OpenAI; handle for completeness.
            choices = result.get("choices") or []
            if choices and isinstance(choices, list):
                msg = (choices[0] or {}).get("message") or {}
                if isinstance(msg.get("content"), str):
                    return msg["content"]
        if isinstance(result, str):
            return result
        raise CloudflareError(
            f"Cloudflare body had no extractable text response: {str(body)[:200]!r}"
        )
