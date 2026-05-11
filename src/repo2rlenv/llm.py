"""LiteLLM wrapper — single entry point across providers, with cost tracking.

The pipelines call `complete(input, prompt)`; we resolve the API key from
either the LLMSpec hint or the provider-default env var, dispatch, then use
LiteLLM's `completion_cost()` to attach a USD estimate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from repo2rlenv.auth import resolve_llm_api_key
from repo2rlenv.spec.input import LLMSpec

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LLMResponse:
    content: str
    usage: dict | None = None
    cost_usd: float = 0.0           # cost of THIS call, in USD (best-effort)
    prompt_tokens: int = 0
    completion_tokens: int = 0


def complete(
    spec: LLMSpec,
    *,
    system: str | None = None,
    user: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
) -> LLMResponse:
    """Single chat-completion call. Honors LLMSpec.endpoint for self-hosted endpoints."""
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
        "temperature": temperature,
        "api_key": api_key,
        "timeout": spec.timeout_sec,
    }
    if spec.endpoint:
        kwargs["api_base"] = spec.endpoint

    if spec.provider == "huggingface" and spec.endpoint is None:
        kwargs.setdefault("api_base", "https://router.huggingface.co/v1")

    response = litellm.completion(**kwargs)
    choice = response.choices[0]
    content = choice.message.content or ""

    # Token counts + cost
    usage_obj = getattr(response, "usage", None)
    prompt_tokens = 0
    completion_tokens = 0
    if usage_obj is not None:
        prompt_tokens = getattr(usage_obj, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage_obj, "completion_tokens", 0) or 0

    cost_usd = 0.0
    try:
        # LiteLLM's completion_cost() reads its built-in model_cost map.
        # Returns 0.0 if the model isn't priced (rare for major providers).
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
