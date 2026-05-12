"""LLM provider failover: 5xx / rate-limit / timeout → spec.fallback retry."""

from __future__ import annotations

from unittest import mock

import pytest

from repo2rlenv.llm import LLMResponse, _is_failover_eligible, complete
from repo2rlenv.spec.input import LLMSpec

# ----------------------------------------------------------------------------
# _is_failover_eligible — exception classification
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_class_name,expected",
    [
        ("InternalServerError", True),  # 5xx including Anthropic 529
        ("RateLimitError", True),
        ("ServiceUnavailableError", True),
        ("APIConnectionError", True),
        ("Timeout", True),
        ("APIError", True),
        ("BadRequestError", False),  # 400 — config bug, don't retry
        ("AuthenticationError", False),  # 401
        ("NotFoundError", False),  # 404 — wrong model
        ("ValueError", False),  # not provider-related at all
    ],
)
def test_failover_eligibility(exc_class_name, expected):
    # Build a synthetic exception with the right class name
    exc = type(exc_class_name, (Exception,), {})("test")
    assert _is_failover_eligible(exc) is expected


# ----------------------------------------------------------------------------
# complete() — fallback behavior
# ----------------------------------------------------------------------------


def _ok_response(content="hello") -> LLMResponse:
    return LLMResponse(content=content, cost_usd=0.001, prompt_tokens=5, completion_tokens=1)


def test_no_fallback_succeeds_first_try():
    spec = LLMSpec(provider="anthropic", model="claude-sonnet-4-6")
    with mock.patch("repo2rlenv.llm._do_complete", return_value=_ok_response("hi")) as m:
        r = complete(spec, user="prompt")
    assert r.content == "hi"
    assert m.call_count == 1


def test_fallback_fires_on_retryable_error():
    primary = LLMSpec(provider="anthropic", model="claude-sonnet-4-6")
    fallback = LLMSpec(provider="openai", model="gpt-5.5")
    primary = primary.model_copy(update={"fallback": fallback})

    overloaded = type("InternalServerError", (Exception,), {})("Overloaded 529")
    calls = []

    def fake_do_complete(spec, **kwargs):
        calls.append(spec.qualified_name)
        if spec.qualified_name == "anthropic/claude-sonnet-4-6":
            raise overloaded
        return _ok_response("from-fallback")

    with mock.patch("repo2rlenv.llm._do_complete", side_effect=fake_do_complete):
        r = complete(primary, user="prompt")

    assert r.content == "from-fallback"
    assert calls == ["anthropic/claude-sonnet-4-6", "openai/gpt-5.5"]


def test_fallback_does_not_fire_on_bad_request():
    """4xx errors signal config bugs (wrong model id, bad params) — re-raise."""
    primary = LLMSpec(provider="anthropic", model="claude-sonnet-4-6")
    fallback = LLMSpec(provider="openai", model="gpt-5.5")
    primary = primary.model_copy(update={"fallback": fallback})

    bad = type("BadRequestError", (Exception,), {})("unknown model")

    with mock.patch("repo2rlenv.llm._do_complete", side_effect=bad):
        with pytest.raises(Exception, match="unknown model"):
            complete(primary, user="prompt")


def test_no_fallback_set_reraises():
    """If spec.fallback is None, retryable errors still propagate."""
    spec = LLMSpec(provider="anthropic", model="claude-sonnet-4-6")  # no fallback
    overloaded = type("InternalServerError", (Exception,), {})("Overloaded")

    with mock.patch("repo2rlenv.llm._do_complete", side_effect=overloaded):
        with pytest.raises(Exception, match="Overloaded"):
            complete(spec, user="prompt")


def test_fallback_chain_caps_recursion():
    """Three nested fallbacks all failing → still re-raises (no infinite loop)."""
    layer3 = LLMSpec(provider="huggingface", model="qwen")
    layer2 = LLMSpec(provider="openai", model="gpt-5.5").model_copy(update={"fallback": layer3})
    layer1 = LLMSpec(provider="anthropic", model="claude-sonnet-4-6").model_copy(
        update={"fallback": layer2}
    )
    # Even self-referential to force depth: layer3.fallback = layer1 (would loop)
    layer3_loop = layer3.model_copy(update={"fallback": layer1})
    layer2 = layer2.model_copy(update={"fallback": layer3_loop})
    layer1 = layer1.model_copy(update={"fallback": layer2})

    overloaded = type("InternalServerError", (Exception,), {})("Overloaded")
    calls = []

    def fake_do_complete(spec, **kwargs):
        calls.append(spec.qualified_name)
        raise overloaded

    with mock.patch("repo2rlenv.llm._do_complete", side_effect=fake_do_complete):
        with pytest.raises(Exception, match="Overloaded"):
            complete(layer1, user="prompt")

    # Should attempt primary + at most 3 fallbacks → 4 total
    assert len(calls) <= 4
