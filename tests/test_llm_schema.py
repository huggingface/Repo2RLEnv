from __future__ import annotations

from types import SimpleNamespace

import litellm
import pytest

from repo2rlenv.llm import complete
from repo2rlenv.spec.input import LLMSpec


def test_schema_reaches_provider_without_changing_unstructured_calls(monkeypatch):
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"issue":"value"}'))],
            usage=None,
        )

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kwargs: 0.01)
    monkeypatch.setattr("repo2rlenv.llm.resolve_llm_api_key", lambda *args: "test-key")
    model = LLMSpec(provider="anthropic", model="claude-sonnet-4-6")
    schema = {
        "type": "object",
        "properties": {"issue": {"type": "string"}},
        "required": ["issue"],
        "additionalProperties": False,
    }
    complete(model, system="system", user="user", response_schema=schema)
    assert calls[0]["response_format"]["json_schema"] == {
        "name": "repo2rlenv_response",
        "strict": True,
        "schema": schema,
    }
    assert calls[0]["messages"][0] == {"role": "system", "content": "system"}
    complete(model, user="user")
    assert "response_format" not in calls[1]


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
def test_current_openai_models_use_completion_limit(monkeypatch, model):
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))], usage=None
        )

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kwargs: 0.01)
    monkeypatch.setattr("repo2rlenv.llm.resolve_llm_api_key", lambda *args: "test-key")
    complete(LLMSpec(provider="openai", model=model), user="test", max_tokens=4096)
    assert calls[0]["max_completion_tokens"] == 4096
    assert "max_tokens" not in calls[0]
    assert "temperature" not in calls[0]
    assert calls[0]["num_retries"] == 0
