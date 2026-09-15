"""Token resolution paths."""

from __future__ import annotations

import os
from unittest import mock

from repo2rlenv.auth import (
    auth_clone_url,
    resolve_github_token,
    resolve_hf_token,
    resolve_llm_api_key,
)
from repo2rlenv.spec.input import AuthSpec, RepoSpec


def test_explicit_env_wins():
    repo = RepoSpec(url="huggingface/trl", auth_token_env="MY_PRIVATE_PAT")
    auth = AuthSpec(use_gh_cli=True)
    with mock.patch.dict(os.environ, {"MY_PRIVATE_PAT": "explicit-token"}):
        assert resolve_github_token(repo, auth) == "explicit-token"


def test_falls_through_to_env_var_when_gh_disabled():
    repo = RepoSpec(url="huggingface/trl")
    auth = AuthSpec(use_gh_cli=False, github_token_env="GITHUB_TOKEN")
    with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "env-token"}, clear=False):
        assert resolve_github_token(repo, auth) == "env-token"


def test_returns_none_when_nothing_set():
    repo = RepoSpec(url="huggingface/trl")
    auth = AuthSpec(use_gh_cli=False, github_token_env="ABSENT_VAR")
    with mock.patch.dict(os.environ, {}, clear=True):
        assert resolve_github_token(repo, auth) is None


def test_clone_url_injects_token():
    url = auth_clone_url("https://github.com/huggingface/trl", "ghp_xxx")
    assert url == "https://x-access-token:ghp_xxx@github.com/huggingface/trl"


def test_clone_url_passthrough_without_token():
    url = auth_clone_url("https://github.com/huggingface/trl", None)
    assert url == "https://github.com/huggingface/trl"


def test_llm_api_key_resolution_uses_provider_default():
    with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-xxx"}):
        assert resolve_llm_api_key("anthropic") == "sk-ant-xxx"


def test_llm_api_key_resolution_explicit_env():
    with mock.patch.dict(os.environ, {"MY_KEY": "custom"}):
        assert resolve_llm_api_key("anthropic", "MY_KEY") == "custom"


def test_hf_token_falls_back_to_env():
    with mock.patch.dict(os.environ, {"HF_TOKEN": "hf_xxx"}, clear=True):
        # Patch the cache file path so we don't accidentally pick up real cache
        auth = AuthSpec(use_hf_cli=False)
        assert resolve_hf_token(auth) == "hf_xxx"


def test_llm_api_key_unknown_provider_returns_none_without_raising():
    """Providers outside LLM_KEY_ENV_DEFAULTS are LiteLLM's to resolve."""
    with mock.patch.dict(os.environ, {}, clear=True):
        assert resolve_llm_api_key("hosted_vllm") is None
        assert resolve_llm_api_key("ollama") is None
        assert resolve_llm_api_key("bedrock") is None


def test_llm_api_key_explicit_env_works_for_unknown_provider():
    with mock.patch.dict(os.environ, {"VLLM_KEY": "secret"}, clear=True):
        assert resolve_llm_api_key("hosted_vllm", "VLLM_KEY") == "secret"


def test_llm_api_key_explicit_env_never_falls_through():
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-personal"}, clear=True):
        assert resolve_llm_api_key("openai", "PROXY_KEY") is None


def test_llm_api_key_blank_counts_as_unset():
    with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=True):
        assert resolve_llm_api_key("anthropic") is None
