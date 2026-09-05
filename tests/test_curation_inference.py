from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from repo2rlenv.curation.agent import IncompleteModelResponse, run_agent
from repo2rlenv.curation.budget import Budget, completion
from repo2rlenv.curation.evaluate import inspect_execution
from repo2rlenv.curation.inference import MAX_OUTPUT_TOKENS, MODEL_TIMEOUT_SEC, inference_settings


class Record:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **kwargs):
        return self.value


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish_reason", [(None, "stop"), ("Partial response", "length")])
async def test_incomplete_agent_response_preserves_state_and_is_not_success(
    tmp_path, monkeypatch, content, finish_reason
):
    pytest.importorskip("langgraph")
    message = {
        "role": "assistant",
        "content": content,
        "thinking_blocks": [{"type": "thinking", "thinking": "", "signature": "opaque"}],
    }

    async def mock_completion(*args, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=Record(message), finish_reason=finish_reason)],
            usage=Record({"completion_tokens": MAX_OUTPUT_TOKENS}),
        ), 0.2

    monkeypatch.setattr("repo2rlenv.curation.agent.completion", mock_completion)
    trace = tmp_path / "agent/trace.jsonl"
    with pytest.raises(IncompleteModelResponse) as exc:
        await run_agent(
            model="anthropic/claude-sonnet-5",
            system="test",
            prompt="test",
            budget=Budget(tmp_path / "budget.json", 10),
            tools=[],
            handlers={},
            trace=trace,
            max_turns=5,
        )
    assert exc.value.state["messages"][-1] == message
    assert exc.value.state["turns"] == 1
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert records[0]["inference"] == inference_settings("anthropic/claude-sonnet-5")
    assert records[-1]["finish_reason"] == finish_reason
    assert inspect_execution(tmp_path).startswith("Incomplete model response")


def test_legacy_thinking_only_trace_fails_execution_audit(tmp_path):
    folder = tmp_path / "agent"
    folder.mkdir()
    (folder / "trace.jsonl").write_text(
        json.dumps(
            {
                "kind": "model",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "thinking_blocks": [{"type": "redacted_thinking", "data": "opaque"}],
                },
                "usage": {"completion_tokens": 6000},
            }
        )
        + "\n"
    )
    assert inspect_execution(tmp_path) == "Incomplete model response: no final text or tool call"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model", ["anthropic/claude-sonnet-5", "anthropic/claude-opus-5", "openai/test"]
)
async def test_explicit_provider_policy_is_budgeted(tmp_path, monkeypatch, model):
    import litellm

    calls = []
    monkeypatch.setattr(
        litellm,
        "get_model_info",
        lambda _: {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002},
    )
    monkeypatch.setattr(litellm, "completion_cost", lambda **kwargs: 0.001)

    async def complete(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", complete)
    budget = Budget(tmp_path / "budget.json", 1)
    await completion(budget, model, [{"role": "user", "content": "test"}])
    assert calls[0]["max_tokens"] == MAX_OUTPUT_TOKENS
    assert calls[0]["timeout"] == MODEL_TIMEOUT_SEC
    if model.startswith("anthropic/"):
        assert calls[0]["thinking"] == {"type": "adaptive"}
        assert calls[0]["output_config"] == {"effort": "medium"}
    else:
        assert "thinking" not in calls[0]
        assert "output_config" not in calls[0]
    assert budget.spent == pytest.approx(0.001)
