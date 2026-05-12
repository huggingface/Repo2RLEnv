"""LiteLLM wrapper — single entry point across providers, with cost tracking.

The pipelines call `complete(input, prompt)`; we resolve the API key from
either the LLMSpec hint or the provider-default env var, dispatch, then use
LiteLLM's `completion_cost()` to attach a USD estimate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from repo2rlenv.auth import resolve_llm_api_key
from repo2rlenv.spec.input import LLMSpec

logger = logging.getLogger(__name__)


# Models that reject any `temperature` value (forced default). Add patterns
# as new releases land. Probed empirically via scripts/probe_llm_routes.py.
_NO_TEMPERATURE_RE = re.compile(
    r"(claude-opus-4-7|claude-opus-4-8|gpt-5(\.|-|$)|gpt-6|o1-|o3-|o4-)",
    re.IGNORECASE,
)


def _supports_temperature(model: str) -> bool:
    return _NO_TEMPERATURE_RE.search(model) is None


@dataclass(slots=True)
class LLMResponse:
    content: str
    usage: dict | None = None
    cost_usd: float = 0.0  # cost of THIS call, in USD (best-effort)
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _is_failover_eligible(exc: BaseException) -> bool:
    """True for transient provider errors worth retrying on a fallback model.

    LiteLLM raises specific subclasses; we match on class names so we don't
    have to import the symbols (some live in nested submodules and shift
    between versions).
    """
    name = type(exc).__name__
    # Retry on: 5xx upstream, rate limits, network blips, timeouts.
    # Don't retry on: 4xx bad-request, auth errors, not-found, content filter.
    return name in {
        "InternalServerError",  # 5xx incl. Anthropic 529 Overloaded
        "RateLimitError",  # 429
        "ServiceUnavailableError",
        "APIConnectionError",
        "Timeout",
        "APIError",  # generic upstream error
    }


def _do_complete(
    spec: LLMSpec,
    *,
    system: str | None,
    user: str,
    max_tokens: int,
    temperature: float,
) -> LLMResponse:
    """One non-fallback chat-completion call. Internal helper for `complete()`."""
    import litellm  # type: ignore[import-untyped]

    api_key = resolve_llm_api_key(spec.provider, spec.api_key_env)
    if api_key is None:
        raise RuntimeError(
            f"no API key resolved for provider {spec.provider!r}. "
            f"Set {spec.api_key_env or 'the provider-default env var'}."
        )

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    kwargs: dict = {
        "model": spec.qualified_name,
        "messages": messages,
        "max_tokens": max_tokens,
        "api_key": api_key,
        "timeout": spec.timeout_sec,
    }
    # Newer reasoning-focused models (Opus 4.7+, GPT-5+) reject `temperature`.
    if _supports_temperature(spec.model):
        kwargs["temperature"] = temperature
    if spec.endpoint:
        kwargs["api_base"] = spec.endpoint

    if spec.provider == "huggingface" and spec.endpoint is None:
        kwargs.setdefault("api_base", "https://router.huggingface.co/v1")

    response = litellm.completion(**kwargs)
    choice = response.choices[0]
    content = choice.message.content or ""

    usage_obj = getattr(response, "usage", None)
    prompt_tokens = 0
    completion_tokens = 0
    if usage_obj is not None:
        prompt_tokens = getattr(usage_obj, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage_obj, "completion_tokens", 0) or 0

    cost_usd = 0.0
    try:
        cost_usd = float(litellm.completion_cost(completion_response=response))
    except Exception as exc:
        logger.debug("completion_cost failed for %s: %s", spec.qualified_name, exc)

    return LLMResponse(
        content=content,
        usage=dict(usage_obj) if usage_obj else None,
        cost_usd=cost_usd,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def complete(
    spec: LLMSpec,
    *,
    system: str | None = None,
    user: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    _depth: int = 0,
) -> LLMResponse:
    """Single chat-completion call with automatic fallback on transient errors.

    Calls `spec.qualified_name`. On 5xx / 429 / network / timeout errors, if
    `spec.fallback` is set, retries with the fallback model recursively (up
    to 3 levels deep, then re-raises). 4xx errors (bad model, auth, etc.)
    are NOT retried — those signal config bugs, not transient failures.

    Honors `LLMSpec.endpoint` for self-hosted backends.
    """
    try:
        return _do_complete(
            spec,
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:
        if _depth >= 3 or spec.fallback is None or not _is_failover_eligible(exc):
            raise
        logger.warning(
            "primary LLM %s failed with %s; falling back to %s",
            spec.qualified_name,
            type(exc).__name__,
            spec.fallback.qualified_name,
        )
        return complete(
            spec.fallback,
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            _depth=_depth + 1,
        )
