from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from repo2rlenv.curation import agent
from repo2rlenv.curation.budget import Budget, BudgetExceeded


def response(content=None, tool_calls=None, finish_reason="stop"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(model_dump=lambda **_: message), finish_reason=finish_reason
            )
        ],
        usage=SimpleNamespace(model_dump=lambda: {}),
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    pytest.importorskip("langgraph")
    budget = Budget(tmp_path / "budget.json", 10)
    calls, responses = [], []

    async def completion(budget, model, messages, **kwargs):
        calls.append({"messages": list(messages), **kwargs})
        if kwargs["max_charge"] < 0.1:
            raise BudgetExceeded("Next call exceeds original allowance")
        reservation = budget.reserve(0.1, "mock-review-call")
        budget.settle(reservation, 0.1)
        return responses.pop(0), 0.1

    monkeypatch.setattr(agent, "completion", completion)
    return SimpleNamespace(
        budget=budget, calls=calls, responses=responses, trace=tmp_path / "trace.jsonl"
    )


async def run(s, validate, *, max_turns=4, max_cost=1, handlers=None):
    return await agent.run_agent(
        model="test",
        system="Review",
        prompt="Read every file",
        budget=s.budget,
        tools=[],
        handlers=handlers or {},
        trace=s.trace,
        max_turns=max_turns,
        max_cost=max_cost,
        validate_final=validate,
    )


@pytest.mark.asyncio
async def test_missing_read_continues_same_conversation_then_preserves_quality_rejection(setup):
    s = setup
    read = False

    async def read_file():
        nonlocal read
        read = True
        return "Remaining file contents"

    final = '{"score": 2, "blockers": ["Missing numerical assertion"]}'
    s.responses.extend(
        [
            response("Premature score"),
            response(
                tool_calls=[
                    {
                        "id": "read-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ]
            ),
            response(final),
        ]
    )
    state = await run(
        s,
        lambda _: None if read else "Read tests/helper.py offset 30",
        handlers={"read_file": read_file},
    )
    assert state["turns"] == 3
    assert state["messages"][-1]["content"] == final
    assert any(m.get("content") == "Premature score" for m in s.calls[-1]["messages"])
    assert s.calls[1]["messages"][-1] == {
        "role": "user",
        "content": "Read tests/helper.py offset 30",
    }
    assert [c["max_charge"] for c in s.calls] == pytest.approx([1, 0.9, 0.8])
    assert s.budget.spent == pytest.approx(0.3)
    events = [json.loads(line) for line in s.trace.read_text().splitlines()]
    assert sum(e["kind"] == "final_validation" for e in events) == 1


@pytest.mark.asyncio
async def test_premature_finals_cannot_reset_original_turn_limit(setup):
    s = setup
    s.responses.extend([response("first"), response("second")])
    with pytest.raises(agent.IncompleteModelResponse, match="turn limit") as caught:
        await run(s, lambda _: "Still missing a file", max_turns=2)
    assert len(s.calls) == caught.value.state["turns"] == 2
    assert caught.value.state["messages"][-1]["content"] == "second"
    assert caught.value.state["cost"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_read_correction_cannot_reset_original_cost_limit(setup):
    s = setup
    s.responses.append(response("premature"))
    with pytest.raises(BudgetExceeded):
        await run(s, lambda _: "Missing file", max_cost=0.15)
    assert len(s.calls) == 2
    assert s.calls[-1]["max_charge"] == pytest.approx(0.05)
    assert s.budget.spent == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_truncated_response_never_enters_final_correction(setup):
    s = setup
    s.responses.append(response("partial", finish_reason="length"))
    callback = AsyncMock()
    with pytest.raises(agent.IncompleteModelResponse, match="token limit"):
        await run(s, callback)
    callback.assert_not_called()
    assert len(s.calls) == 1


@pytest.mark.asyncio
async def test_external_runtime_cannot_silently_ignore_final_validator(tmp_path):
    with pytest.raises(ValueError, match="requires the LangGraph runtime"):
        await agent.run_agent(
            model="test",
            system="s",
            prompt="p",
            budget=Budget(tmp_path / "budget.json", 1),
            tools=[],
            handlers={},
            trace=tmp_path / "trace.jsonl",
            max_turns=2,
            runtime="pi",
            validate_final=lambda _: "missing",
        )


@pytest.mark.asyncio
async def test_output_allowance_reaches_metered_completion_and_recorded_inference(setup):
    s = setup
    s.responses.append(response("complete"))
    await agent.run_agent(
        model="test",
        system="s",
        prompt="p",
        budget=s.budget,
        tools=[],
        handlers={},
        trace=s.trace,
        max_turns=2,
        max_cost=1,
        max_output_tokens=32_000,
    )
    assert s.calls[0]["max_tokens"] == 32_000
    assert s.calls[0]["max_charge"] == 1
    recorded = json.loads(s.trace.read_text().splitlines()[0])
    assert recorded["inference"]["max_tokens"] == 32_000
    assert agent.inference_settings("test")["max_tokens"] == 16_000


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens", [True, 0, -1, 128_001])
async def test_invalid_output_allowance_is_rejected_before_execution(tmp_path, tokens):
    with pytest.raises(ValueError, match="Output token limit"):
        await agent.run_agent(
            model="test",
            system="s",
            prompt="p",
            budget=Budget(tmp_path / "budget.json", 1),
            tools=[],
            handlers={},
            trace=tmp_path / "trace.jsonl",
            max_turns=2,
            max_output_tokens=tokens,
        )
    assert not (tmp_path / "trace.jsonl").exists()


@pytest.mark.asyncio
async def test_external_runtime_cannot_silently_ignore_output_override(tmp_path):
    with pytest.raises(ValueError, match="Output token overrides"):
        await agent.run_agent(
            model="test",
            system="s",
            prompt="p",
            budget=Budget(tmp_path / "budget.json", 1),
            tools=[],
            handlers={},
            trace=tmp_path / "trace.jsonl",
            max_turns=2,
            runtime="pi",
            max_output_tokens=32_000,
        )
