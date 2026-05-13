"""Failover provider — orchestrates an ordered chain of classifier providers.

The agent's primary provider (Mistral free-tier) is rate-limited and can hit
the 429 ceiling under burst load (cf. the audit_events captured in S299).
This wrapper lets the orchestrator declare an ordered chain of providers and
recover gracefully when the primary is unhealthy.

Behaviour:
- Try each provider in order, starting from the primary.
- A provider that raises a transient failure (HTTP errors, rate limits,
  schema errors) is marked **unhealthy** for ``cooldown_seconds``; further
  calls skip it until the cooldown expires.
- When all providers in the chain are unhealthy, the *least recently failed*
  one is given a sympathy retry — better to risk a 429 than crash the tick.
- Every provider-switch (and every cooldown engagement) emits a structured
  log line ``FAILOVER`` that an orchestrator-level audit hook can pick up.

Failure-type taxonomy:
- :class:`CloudflareError` / :class:`MistralError` / network errors →
  mark unhealthy, advance to next
- :class:`ClassifierSchemaError` → advance to next for THIS call but do NOT
  mark unhealthy (it's a model quality blip, not provider availability)
- Any other ``Exception`` → propagate (programmer errors, config mistakes)
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from ..classifier import Classification, ClassifierSchemaError, PostRecord

log = logging.getLogger(__name__)


class FailoverExhausted(RuntimeError):
    """All providers in the chain failed for a single classify() call."""


# Errors that mark a provider as unhealthy (cooldown engaged).
_HEALTH_FATAL_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    httpx.InvalidURL,
    OSError,
)


def _is_provider_error(exc: BaseException) -> bool:
    """Best-effort: detect provider-specific RuntimeErrors by name to avoid
    a hard import dependency on every provider's exception class.

    The provider modules raise ``MistralError`` and ``CloudflareError`` which
    inherit from ``RuntimeError``. We check class name so this module stays
    importable even when one provider's deps are missing.
    """
    cls = type(exc)
    return cls.__name__ in ("MistralError", "CloudflareError") or (
        isinstance(exc, RuntimeError) and cls.__name__.endswith("Error")
        and cls is not RuntimeError
        and cls.__module__.startswith("seeker_agent.providers")
    )


class FailoverProvider:
    """Wraps multiple providers; tries each in order on classify().

    ``name`` is the comma-joined chain name so audit rows show the choice set:
    e.g. ``"mistral+cloudflare"``.
    """

    def __init__(
        self,
        providers: list,
        cooldown_seconds: float = 300.0,
    ) -> None:
        if not providers:
            raise ValueError("FailoverProvider requires at least one provider")
        self._providers = providers
        self._cooldown = cooldown_seconds
        # provider.name -> monotonic timestamp when cooldown expires
        self._cooldown_until: dict[str, float] = {}
        self._lock = threading.Lock()
        self.name = "+".join(getattr(p, "name", "?") for p in providers)

    def _is_healthy(self, provider_name: str, now: float) -> bool:
        return self._cooldown_until.get(provider_name, 0.0) <= now

    def _mark_unhealthy(self, provider_name: str, reason: str) -> None:
        with self._lock:
            self._cooldown_until[provider_name] = time.monotonic() + self._cooldown
        log.warning(
            "FAILOVER provider=%s unhealthy cooldown=%.0fs reason=%s",
            provider_name,
            self._cooldown,
            reason,
        )

    def _mark_healthy(self, provider_name: str) -> None:
        """Called after a successful call — clear any cooldown."""
        with self._lock:
            self._cooldown_until.pop(provider_name, None)

    def classify(self, post: PostRecord) -> Classification:
        now = time.monotonic()

        # Build the order: healthy providers first, then unhealthy in
        # least-recently-marked order (last resort if everyone is sick).
        healthy: list = []
        unhealthy: list = []
        for p in self._providers:
            if self._is_healthy(getattr(p, "name", ""), now):
                healthy.append(p)
            else:
                unhealthy.append(p)
        # least-recently-marked first within unhealthy
        unhealthy.sort(key=lambda p: self._cooldown_until.get(getattr(p, "name", ""), 0.0))
        chain = healthy + unhealthy

        last_exc: BaseException | None = None
        for i, p in enumerate(chain):
            pname = getattr(p, "name", f"provider_{i}")
            is_resort = p in unhealthy
            try:
                result = p.classify(post)
                # Success — clear any cooldown and emit if we used a non-primary.
                self._mark_healthy(pname)
                if i > 0:
                    log.info(
                        "FAILOVER provider=%s succeeded (slot=%d, was_unhealthy=%s)",
                        pname, i, is_resort,
                    )
                return result
            except ClassifierSchemaError as exc:
                # Single-call hiccup — try the next provider for this post,
                # but do NOT mark the provider unhealthy. This kind of error
                # is a model-quality blip, not an availability issue.
                log.warning(
                    "FAILOVER provider=%s schema_error=%s; trying next",
                    pname, str(exc)[:200],
                )
                last_exc = exc
                continue
            except _HEALTH_FATAL_ERRORS as exc:
                self._mark_unhealthy(pname, f"{type(exc).__name__}: {str(exc)[:120]}")
                last_exc = exc
                continue
            except Exception as exc:
                if _is_provider_error(exc):
                    self._mark_unhealthy(pname, f"{type(exc).__name__}: {str(exc)[:120]}")
                    last_exc = exc
                    continue
                # Programmer error — let it propagate.
                raise

        # All providers failed.
        msg = (
            f"all {len(chain)} providers exhausted; last error: "
            f"{type(last_exc).__name__ if last_exc else 'unknown'}: {str(last_exc)[:200]}"
        )
        raise FailoverExhausted(msg) from last_exc
