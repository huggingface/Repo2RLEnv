from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from repo2rlenv.curation import design
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from tests.test_curation_design import valid_design


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["langgraph", "pi", "opencode"])
async def test_exhausted_exploration_reserves_synthesis_with_same_ledger(
    tmp_path, monkeypatch, runtime
):
    budget = Budget(tmp_path / "ledger.json", 40, scope="candidate", scope_limit=8)
    previous = budget.reserve(3, "prior-candidate-work")
    budget.settle(previous, 3)
    calls = []
    shell = AsyncMock(return_value="independent reference: margin 2.5 then -1.0")

    async def agent(**kwargs):
        calls.append(kwargs)
        assert kwargs["budget"] is budget and kwargs["runtime"] == runtime
        if len(calls) == 1:
            assert kwargs["max_turns"] == 12 and kwargs["max_cost"] == pytest.approx(1.2)
            await kwargs["handlers"]["shell"]("read the changed methods")
            for _ in range(12):
                key = budget.reserve(0.1, "exploration-model")
                budget.settle(key, 0.1)
            if runtime != "langgraph":
                raise BudgetExceeded("Runtime model-call limit reached: 12")
            return {"turns": 12, "cost": 1.2, "messages": []}
        assert kwargs["max_turns"] == 8
        assert kwargs["max_cost"] == pytest.approx(0.8)
        assert list(kwargs["handlers"]) == ["submit_design"]
        assert [t["function"]["name"] for t in kwargs["tools"]] == ["submit_design"]
        assert "margin 2.5 then -1.0" in kwargs["prompt"]
        assert "read the changed methods" in kwargs["prompt"]
        assert "schema validation failed" in await kwargs["handlers"]["submit_design"](
            task_request="x"
        )
        assert not (tmp_path / "design.json").exists()
        for _ in range(8):
            key = budget.reserve(0.1, "synthesis-model")
            budget.settle(key, 0.1)
        await kwargs["handlers"]["submit_design"](**valid_design())
        assert json.loads((tmp_path / "design.json").read_text())["design"] == valid_design()
        return {"turns": 8, "cost": 0.8, "messages": []}

    monkeypatch.setattr(design, "run_agent", agent)
    accepted = await design.plan_candidate_design(
        source={}, root=tmp_path, shell=shell, budget=budget, model="mock", runtime=runtime
    )
    assert accepted.model_dump() == valid_design()
    assert len(calls) == 2 and sum(c["max_turns"] for c in calls) == 20
    assert budget.spent == pytest.approx(5)
    entries = json.loads(budget.path.read_text())["entries"]
    assert len(entries) == 21
    assert {e["scope"] for e in entries.values()} == {"candidate"}
    assert all(e["status"] == "metered" for e in entries.values())
    shell.assert_awaited_once()
    assert calls[0]["trace"].name == "design.jsonl"
    assert calls[1]["trace"].name == "design-synthesis.jsonl"
    receipt = json.loads((tmp_path / "design-phases.json").read_text())
    assert receipt["total_turn_cap"] == 20
    assert receipt["exploration_turn_cap"] == 12 and receipt["synthesis_turn_cap"] == 8
    assert receipt["reserved_synthesis_cost_usd"] == pytest.approx(0.8)
    assert receipt["synthesis_attempted"] and receipt["synthesis_outcome"] == "accepted"
    assert receipt["committed_delta_usd"] == pytest.approx(2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Next model request would exceed the agent cost limit",
        "Agent cost limit reached: $1.2",
        "Runtime agent budget $1.2: $1.0000 committed; need $0.3000",
    ],
)
async def test_known_local_cost_gate_transitions_without_resetting_charges(
    tmp_path, monkeypatch, message
):
    budget = Budget(tmp_path / "ledger", 40)
    calls = []

    async def agent(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            # Unknown provider usage stays reserved and reduces synthesis headroom.
            budget.reserve(1.1, "outstanding")
            raise BudgetExceeded(message)
        assert kwargs["max_cost"] == pytest.approx(0.9)
        await kwargs["handlers"]["submit_design"](**valid_design())

    monkeypatch.setattr(design, "run_agent", agent)
    await design.plan_candidate_design(
        source={}, root=tmp_path, shell=AsyncMock(), budget=budget, model="m"
    )
    assert budget.spent == 1.1 and len(calls) == 2
    assert (
        next(iter(json.loads(budget.path.read_text())["entries"].values()))["status"] == "reserved"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Budget $40.00: $39.80 committed; need $0.30",
        "Candidate budget $8.00: $7.80 committed; need $0.30",
        "Batch budget $40.00: $39.80 committed; need $0.30",
        "Runtime model-call limit reached: 20",
    ],
)
async def test_other_budget_errors_do_not_start_synthesis(tmp_path, monkeypatch, message):
    agent = AsyncMock(side_effect=BudgetExceeded(message))
    monkeypatch.setattr(design, "run_agent", agent)
    with pytest.raises(BudgetExceeded, match=r"budget|Budget|limit"):
        await design.plan_candidate_design(
            source={},
            root=tmp_path,
            shell=AsyncMock(),
            budget=Budget(tmp_path / "ledger", 40),
            model="m",
        )
    agent.assert_awaited_once()
    receipt = json.loads((tmp_path / "design-phases.json").read_text())
    assert receipt["outcome"] == "failed" and receipt["error"] == message
    assert not receipt["synthesis_attempted"]


@pytest.mark.asyncio
async def test_single_turn_cap_goes_directly_to_submission_without_shell(tmp_path, monkeypatch):
    async def agent(**kwargs):
        assert kwargs["max_turns"] == 1 and kwargs["max_cost"] == 0.4
        assert list(kwargs["handlers"]) == ["submit_design"]
        await kwargs["handlers"]["submit_design"](**valid_design())

    monkeypatch.setattr(design, "run_agent", agent)
    shell = AsyncMock()
    await design.plan_candidate_design(
        source={},
        root=tmp_path,
        shell=shell,
        budget=Budget(tmp_path / "ledger", 40),
        model="m",
        max_turns=1,
        max_cost=0.4,
    )
    shell.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_langgraph_loops_stop_exploring_and_retain_evidence(tmp_path, monkeypatch):
    pytest.importorskip("langgraph")
    from repo2rlenv.curation import agent

    calls = []
    budget = Budget(tmp_path / "ledger", 40)

    async def completion(actual_budget, model, history, **kwargs):
        assert actual_budget is budget
        names = [t["function"]["name"] for t in kwargs["tools"]]
        calls.append((names, kwargs["max_charge"]))
        if "shell" in names:
            msg = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(len(calls)),
                        "type": "function",
                        "function": {"name": "shell", "arguments": '{"command":"read source"}'},
                    }
                ],
            }
        else:
            assert "observed gradient is 3" in history[1]["content"]
            msg = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "submit",
                        "type": "function",
                        "function": {
                            "name": "submit_design",
                            "arguments": json.dumps(valid_design()),
                        },
                    }
                ],
            }
        charge = budget.reserve(0.1, "mock-provider")
        budget.settle(charge, 0.1)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(model_dump=lambda **_: msg), finish_reason="tool_calls"
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: {}),
        ), 0.1

    monkeypatch.setattr(agent, "completion", completion)
    shell = AsyncMock(return_value="observed gradient is 3")
    result = await design.plan_candidate_design(
        source={}, root=tmp_path, shell=shell, budget=budget, model="mock", max_turns=5, max_cost=1
    )
    assert result.model_dump() == valid_design()
    assert len(calls) == 5  # exploration 3 + synthesis 2; no allowance reset
    assert shell.await_count == 3
    assert calls[3][1] == pytest.approx(0.7)
    assert budget.spent == pytest.approx(0.5)
    for filename, count in [("design.jsonl", 3), ("design-synthesis.jsonl", 2)]:
        rows = [json.loads(line) for line in (tmp_path / filename).read_text().splitlines()]
        assert sum(r["kind"] == "model" for r in rows) == count


@pytest.mark.asyncio
async def test_synthesis_evidence_is_bounded_but_original_outputs_retained(tmp_path, monkeypatch):
    calls = []
    output = "BEGIN_OBSERVATION" + "x" * 150000 + "END_OBSERVATION"

    async def agent(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            await kwargs["handlers"]["shell"]("read source")
            return {"messages": [], "turns": 1, "cost": 0}
        assert len(kwargs["prompt"]) < design.MAX_SYNTHESIS_EVIDENCE_CHARS + 1000
        assert "middle omitted" in kwargs["prompt"]
        assert "BEGIN_OBSERVATION" in kwargs["prompt"] and "END_OBSERVATION" in kwargs["prompt"]
        await kwargs["handlers"]["submit_design"](**valid_design())

    monkeypatch.setattr(design, "run_agent", agent)
    await design.plan_candidate_design(
        source={},
        root=tmp_path,
        shell=AsyncMock(return_value=output),
        budget=Budget(tmp_path / "ledger", 40),
        model="m",
    )
    first = json.loads((tmp_path / "design-evidence.jsonl").read_text().splitlines()[0])
    assert first["output"] == output


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["pi", "opencode"])
async def test_native_dispatch_real_bridge_turn_gate_keeps_reserved_synthesis(
    tmp_path, monkeypatch, runtime
):
    pytest.importorskip("aiohttp")
    litellm = pytest.importorskip("litellm")
    from repo2rlenv.curation import external_agent
    from repo2rlenv.curation.bridge import AgentBridge

    monkeypatch.setattr(
        litellm,
        "get_model_info",
        lambda _: {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
        },
    )
    budget = Budget(tmp_path / "ledger", 40)
    allocations = []

    async def native(**kwargs):
        assert kwargs["engine"] == runtime
        allocations.append(kwargs["max_turns"])
        if len(allocations) == 1:
            bridge = AgentBridge(
                **{
                    key: kwargs[key]
                    for key in (
                        "model",
                        "budget",
                        "tools",
                        "handlers",
                        "trace",
                        "max_turns",
                        "max_cost",
                    )
                }
            )
            bridge.turns = kwargs["max_turns"]
            for _ in range(bridge.turns):
                key = budget.reserve(0.05, "retained-native-request")
                budget.settle(key, 0.05)
            # Exercise the actual native bridge refusal before any HTTP provider call.
            request = SimpleNamespace(
                json=AsyncMock(
                    return_value={
                        "model": "claude-sonnet-5",
                        "messages": [],
                        "tools": [],
                    }
                )
            )
            await bridge._messages(request)
            pytest.fail("native turn cap must stop this exploration loop")
        assert kwargs["max_cost"] == pytest.approx(1.4)
        assert "shell" not in kwargs["handlers"]
        await kwargs["handlers"]["submit_design"](**valid_design())
        return {"turns": 1, "messages": [], "cost": 0}

    monkeypatch.setattr(external_agent, "run_external_agent", native)
    await design.plan_candidate_design(
        source={},
        root=tmp_path,
        shell=AsyncMock(),
        budget=budget,
        model="anthropic/claude-sonnet-5",
        runtime=runtime,
    )
    assert allocations == [12, 8]
    assert budget.spent == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_synthesis_budget_failure_propagates_without_third_attempt(tmp_path, monkeypatch):
    calls = []

    async def agent(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"turns": 1, "cost": 0, "messages": []}
        raise BudgetExceeded("Candidate budget $8.00: $7.90 committed; need $0.30")

    monkeypatch.setattr(design, "run_agent", agent)
    with pytest.raises(BudgetExceeded, match="Candidate budget"):
        await design.plan_candidate_design(
            source={},
            root=tmp_path,
            shell=AsyncMock(),
            budget=Budget(tmp_path / "ledger", 40),
            model="m",
        )
    assert len(calls) == 2 and not (tmp_path / "design.json").exists()
