"""API-key resolution for self-hosted / keyless backends (vLLM, Ollama, …).

No live LLM and no real `litellm` import — `_do_complete` imports it lazily, so a
stub module in `sys.modules` stands in and its `completion` kwargs are inspected.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from repo2rlenv.llm import _PLACEHOLDER_API_KEY, _do_complete, check_provider
from repo2rlenv.spec.input import LLMSpec

ENDPOINT = "http://127.0.0.1:8000/v1"


def _fake_response(content: str = "PONG") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=None,
    )


@pytest.fixture
def litellm_stub(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    stub = SimpleNamespace(
        completion=mock.MagicMock(return_value=_fake_response()),
        completion_cost=lambda **kw: 0.0,
        get_llm_provider=mock.MagicMock(return_value=("m", "p", None, None)),
    )
    monkeypatch.setitem(sys.modules, "litellm", stub)
    return stub


def _call(spec: LLMSpec) -> dict:
    return _do_complete(spec, system=None, user="ping", max_tokens=8, temperature=0.0)


def _kwargs(stub: SimpleNamespace) -> dict:
    assert stub.completion.call_count == 1
    return stub.completion.call_args.kwargs


# --- endpoint set --------------------------------------------------------------


@mock.patch.dict(os.environ, {}, clear=True)
def test_hosted_vllm_endpoint_leaves_key_to_litellm(litellm_stub):
    spec = LLMSpec(provider="hosted_vllm", model="Qwen/Qwen3.5-4B", endpoint=ENDPOINT)
    resp = _call(spec)
    kw = _kwargs(litellm_stub)
    assert resp.content == "PONG"
    assert kw["model"] == "hosted_vllm/Qwen/Qwen3.5-4B"
    assert kw["api_base"] == ENDPOINT
    assert "api_key" not in kw  # LiteLLM reads HOSTED_VLLM_API_KEY itself, or fakes one


@mock.patch.dict(os.environ, {"HOSTED_VLLM_API_KEY": "s3cret"}, clear=True)
def test_hosted_vllm_env_key_not_shadowed(litellm_stub):
    """A server started with `--api-key` and HOSTED_VLLM_API_KEY set must not get a placeholder."""
    _call(LLMSpec(provider="hosted_vllm", model="m", endpoint=ENDPOINT))
    assert "api_key" not in _kwargs(litellm_stub)


@mock.patch.dict(os.environ, {}, clear=True)
def test_openai_compat_endpoint_gets_placeholder(litellm_stub):
    """`openai/<model>` + endpoint — the OpenAI client needs *a* key string."""
    _call(LLMSpec(provider="openai", model="Qwen/Qwen3.5-4B", endpoint=ENDPOINT))
    kw = _kwargs(litellm_stub)
    assert kw["api_base"] == ENDPOINT
    assert kw["api_key"] == _PLACEHOLDER_API_KEY


@mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-live"}, clear=True)
def test_default_key_never_forwarded_to_custom_endpoint(litellm_stub):
    _call(LLMSpec(provider="openai", model="m", endpoint=ENDPOINT))
    assert _kwargs(litellm_stub)["api_key"] == _PLACEHOLDER_API_KEY


@mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-live"}, clear=True)
def test_explicit_key_env_is_forwarded_to_endpoint(litellm_stub):
    spec = LLMSpec(provider="openai", model="m", endpoint=ENDPOINT, api_key_env="OPENAI_API_KEY")
    _call(spec)
    assert _kwargs(litellm_stub)["api_key"] == "sk-live"


@mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=True)
def test_blank_env_var_counts_as_unset(litellm_stub):
    """`.env.example` ships `OPENAI_API_KEY=`; that must not become api_key=''."""
    _call(LLMSpec(provider="openai", model="m", endpoint=ENDPOINT))
    assert _kwargs(litellm_stub)["api_key"] == _PLACEHOLDER_API_KEY


# --- no endpoint ---------------------------------------------------------------


@mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-live"}, clear=True)
def test_named_but_unset_key_env_does_not_fall_through(litellm_stub):
    spec = LLMSpec(provider="openai", model="m", api_key_env="PROXY_KEY")
    with pytest.raises(RuntimeError, match=r"\$PROXY_KEY is unset"):
        _call(spec)
    litellm_stub.completion.assert_not_called()


@mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=True)
def test_hosted_provider_without_key_fails_fast_naming_env_var(litellm_stub):
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _call(LLMSpec(provider="openai", model="gpt-5.5"))
    litellm_stub.completion.assert_not_called()


@mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant"}, clear=True)
def test_hosted_provider_default_key_forwarded(litellm_stub):
    _call(LLMSpec(provider="anthropic", model="claude-sonnet-4-6"))
    kw = _kwargs(litellm_stub)
    assert kw["api_key"] == "sk-ant"
    assert "api_base" not in kw


@mock.patch.dict(os.environ, {}, clear=True)
@pytest.mark.parametrize("provider", ["hosted_vllm", "ollama", "bedrock", "vertex_ai"])
def test_other_providers_are_litellms_to_resolve(litellm_stub, provider):
    _call(LLMSpec(provider=provider, model="m"))
    kw = _kwargs(litellm_stub)
    assert "api_key" not in kw
    assert "api_base" not in kw


@mock.patch.dict(os.environ, {"HF_TOKEN": "hf_x"}, clear=True)
def test_huggingface_router_default_base_unchanged(litellm_stub):
    _call(LLMSpec(provider="huggingface", model="org/model:together"))
    kw = _kwargs(litellm_stub)
    assert kw["api_key"] == "hf_x"
    assert kw["api_base"] == "https://router.huggingface.co/v1"


# --- check_provider ------------------------------------------------------------


def test_check_provider_walks_fallback_chain(litellm_stub):
    fb = LLMSpec(provider="openai", model="gpt-5.5")
    spec = LLMSpec(provider="anthropic", model="claude-sonnet-4-6", fallback=fb)
    check_provider(spec)
    seen = [c.args[0] for c in litellm_stub.get_llm_provider.call_args_list]
    assert seen == ["anthropic/claude-sonnet-4-6", "openai/gpt-5.5"]


def test_check_provider_names_the_bad_spec(litellm_stub):
    litellm_stub.get_llm_provider.side_effect = ValueError("LLM Provider NOT provided\nmore")
    with pytest.raises(RuntimeError, match=r"'antropic/claude'.*NOT provided$"):
        check_provider(LLMSpec(provider="antropic", model="claude"))
