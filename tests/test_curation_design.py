from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from repo2rlenv.curation import design
from repo2rlenv.curation.budget import Budget, BudgetExceeded


def valid_design():
    return {
        "task_request": "Make adapter injection select the modules named by its checkpoint, preserving unrelated modules and accepting compiled targets.",
        "verification_plan": {
            "behaviors": [
                {
                    "requirement": name,
                    "expected_result": "Use two distinct Linear shapes and a checkpoint selecting only one; derive LoRA shapes from fixed rank and dimensions.",
                    "tests": ["test_" + name],
                    "mutations": ["ignore_checkpoint"],
                    "equivalents": ["conditional_prefix_slice"],
                }
                for name in ("checkpoint_targets", "compiled_targets")
            ],
            "offline_dependencies": "Pinned CPU Torch and locally constructed two-Linear models without downloaded checkpoints.",
            "artifact_boundary": "Editable src/peft includes injection and helper modules; protected tests are excluded.",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["langgraph", "pi", "opencode"])
async def test_schema_failure_then_acceptance_persists_before_build(tmp_path, monkeypatch, runtime):
    budget = Budget(tmp_path / "shared.json", 40, scope="candidate", scope_limit=8)
    root = tmp_path / "candidate"
    shell = AsyncMock(return_value="remote source inspected")
    payload = valid_design()
    captured = {}

    async def agent(**kwargs):
        captured.update(kwargs)
        assert kwargs["budget"] is budget
        assert kwargs["runtime"] == runtime
        assert kwargs["max_turns"] == 20 and kwargs["max_cost"] == 2
        schema = kwargs["tools"][1]["function"]["parameters"]
        assert schema["properties"]["task_request"]["minLength"] == 50
        assert schema["additionalProperties"] is False
        assert schema["$defs"]["BehaviorDesign"]["properties"]["mutations"]["minItems"] == 1
        handlers = kwargs["handlers"]
        assert await handlers["shell"]("inspect source") == "remote source inspected"
        broken = copy.deepcopy(payload)
        broken["verification_plan"]["behaviors"][0]["mutations"] = []
        feedback = await handlers["submit_design"](**broken)
        assert "schema validation failed" in feedback and "mutations" in feedback
        assert not (root / "design.json").exists()
        assert "accepted" in await handlers["submit_design"](**payload)
        # The submit response is returned only after the durable design exists.
        assert json.loads((root / "design.json").read_text())["design"] == payload
        assert not (root / "submitted-drafts.json").exists()
        kwargs["trace"].write_text('{"kind":"mocked native trace"}\n')
        # A native runtime may emit queued calls after acceptance: no mutations.
        assert "closed" in await handlers["shell"]("touch /output/task/bad")
        changed = copy.deepcopy(payload)
        changed["task_request"] += " Changed after acceptance."
        assert "replacement is closed" in await handlers["submit_design"](**changed)
        return {"messages": [{"role": "assistant", "content": "Planning complete."}]}

    monkeypatch.setattr(design, "run_agent", agent)
    accepted = await design.plan_candidate_design(
        source={"url": "https://github.com/org/repo/pull/1"},
        root=root,
        shell=shell,
        budget=budget,
        model="model",
        runtime=runtime,
    )
    assert accepted.model_dump() == payload
    shell.assert_awaited_once_with(command="inspect source", timeout_sec=120)
    assert captured["trace"] == root / "design.jsonl"
    assert captured["trace"].read_text() == '{"kind":"mocked native trace"}\n'
    assert json.loads((root / "design.json").read_text())["design"] == accepted.model_dump()


@pytest.mark.asyncio
async def test_no_accepted_design_is_explicit_failure(tmp_path, monkeypatch):
    async def agent(**kwargs):
        assert "schema validation failed" in await kwargs["handlers"]["submit_design"](
            task_request="tiny"
        )
        return {"messages": [{"role": "assistant", "content": "I am finished."}]}

    monkeypatch.setattr(design, "run_agent", agent)
    with pytest.raises(design.DesignNotSubmitted, match="implementation must not start"):
        await design.plan_candidate_design(
            source={},
            root=tmp_path,
            shell=AsyncMock(),
            budget=Budget(tmp_path / "ledger", 40),
            model="m",
        )
    assert not (tmp_path / "design.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted_first", [False, True])
async def test_budget_exception_propagates_even_after_submit(tmp_path, monkeypatch, accepted_first):
    async def agent(**kwargs):
        if accepted_first:
            await kwargs["handlers"]["submit_design"](**valid_design())
        raise BudgetExceeded("original shared budget exhausted")

    monkeypatch.setattr(design, "run_agent", agent)
    with pytest.raises(BudgetExceeded, match="original shared budget"):
        await design.plan_candidate_design(
            source={},
            root=tmp_path,
            shell=AsyncMock(),
            budget=Budget(tmp_path / "ledger", 40),
            model="m",
        )
    assert (tmp_path / "design.json").exists() is accepted_first


@pytest.mark.asyncio
async def test_existing_design_resumes_without_agent_or_shell(tmp_path, monkeypatch):
    (tmp_path / "design.json").write_text(
        json.dumps(
            {
                "source_digest": hashlib.sha256(b"{}").hexdigest(),
                "design": valid_design(),
            }
        )
    )
    agent, shell = AsyncMock(), AsyncMock()
    monkeypatch.setattr(design, "run_agent", agent)
    result = await design.plan_candidate_design(
        source={},
        root=tmp_path,
        shell=shell,
        budget=Budget(tmp_path / "ledger", 40),
        model="m",
    )
    assert result.model_dump() == valid_design()
    agent.assert_not_awaited()
    shell.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_storage_does_not_accept_or_start_build(tmp_path, monkeypatch):
    def unavailable(*args):
        raise OSError("storage unavailable")

    async def agent(**kwargs):
        await kwargs["handlers"]["submit_design"](**valid_design())
        pytest.fail("failed write must propagate")

    monkeypatch.setattr(design, "_save_design", unavailable)
    monkeypatch.setattr(design, "run_agent", agent)
    with pytest.raises(OSError, match="storage unavailable"):
        await design.plan_candidate_design(
            source={},
            root=tmp_path,
            shell=AsyncMock(),
            budget=Budget(tmp_path / "ledger", 40),
            model="m",
        )
    assert not (tmp_path / "design.json").exists()


@pytest.mark.asyncio
async def test_inflight_shell_completes_before_acceptance_queued_shell_is_blocked(
    tmp_path, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def shell(**kwargs):
        calls.append(kwargs["command"])
        entered.set()
        await release.wait()
        return "read complete"

    async def agent(**kwargs):
        h = kwargs["handlers"]
        first = asyncio.create_task(h["shell"]("read source"))
        await entered.wait()
        submit = asyncio.create_task(h["submit_design"](**valid_design()))
        await asyncio.sleep(0)
        after = asyncio.create_task(h["shell"]("write after accept"))
        release.set()
        assert await first == "read complete"
        assert "accepted" in await submit
        assert "closed" in await after

    monkeypatch.setattr(design, "run_agent", agent)
    await design.plan_candidate_design(
        source={},
        root=tmp_path,
        shell=shell,
        budget=Budget(tmp_path / "ledger", 40),
        model="m",
    )
    assert calls == ["read source"]


def test_duplicate_requirement_and_blank_request_rejected():
    payload = valid_design()
    payload["verification_plan"]["behaviors"][1]["requirement"] = "checkpoint_targets"
    with pytest.raises(ValueError, match="exactly once"):
        design.CandidateDesign.model_validate(payload)
    payload = valid_design()
    payload["task_request"] = " " * 100
    with pytest.raises(ValueError):
        design.CandidateDesign.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [{"max_turns": 21}, {"max_cost": 3}, {"max_cost": float("nan")}])
async def test_cannot_expand_phase_caps(tmp_path, monkeypatch, limits):
    agent = AsyncMock()
    monkeypatch.setattr(design, "run_agent", agent)
    with pytest.raises(ValueError):
        await design.plan_candidate_design(
            source={},
            root=tmp_path,
            shell=AsyncMock(),
            budget=Budget(tmp_path / "ledger", 40),
            model="m",
            **limits,
        )
    agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_agent_loop_keeps_schema_feedback_trace_and_shared_costs(tmp_path, monkeypatch):
    """Exercise actual model/tool transitions, mocking only provider completion."""
    from types import SimpleNamespace

    from repo2rlenv.curation import agent

    pytest.importorskip("langgraph")
    payload = valid_design()
    bad = copy.deepcopy(payload)
    bad["verification_plan"]["behaviors"][0]["mutations"] = []
    messages = []
    for i, arguments in enumerate((bad, payload)):
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(i),
                        "type": "function",
                        "function": {
                            "name": "submit_design",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
        )
    messages.append(
        {"role": "assistant", "content": "Design saved; ready for the separate build phase."}
    )
    budget = Budget(tmp_path / "shared.json", 40, scope="candidate", scope_limit=8)
    seen = []

    async def completion(actual_budget, model, history, **kwargs):
        assert actual_budget is budget
        seen.append(list(history))
        charge = budget.reserve(0.05, "mock-design-completion")
        budget.settle(charge, 0.05)
        msg = messages.pop(0)
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(model_dump=lambda **_: msg),
                    finish_reason="tool_calls" if "tool_calls" in msg else "stop",
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: {}),
        )
        return response, 0.05

    monkeypatch.setattr(agent, "completion", completion)
    shell = AsyncMock()
    root = tmp_path / "candidate"
    result = await design.plan_candidate_design(
        source={},
        root=root,
        shell=shell,
        budget=budget,
        model="mock-model",
    )
    assert result.model_dump() == payload
    assert budget.spent == pytest.approx(0.15)
    assert "schema validation failed" in seen[1][-1]["content"]
    assert "accepted and saved" in seen[2][-1]["content"]
    trace = [json.loads(line) for line in (root / "design.jsonl").read_text().splitlines()]
    assert len([r for r in trace if r["kind"] == "model"]) == 3
    assert len([r for r in trace if r["kind"] == "tool"]) == 2
    assert not (root / "submitted-drafts.json").exists()
    shell.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_binding_is_canonical_and_published_with_design(tmp_path, monkeypatch):
    source = {
        "url": "https://github.com/org/repo/pull/1",
        "base_sha": "abc",
        "screening": {"b": 2, "a": "é"},
    }

    async def agent(**kwargs):
        await kwargs["handlers"]["submit_design"](**valid_design())
        envelope = json.loads((tmp_path / "design.json").read_text())
        expected = hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        assert envelope == {"source_digest": expected, "design": valid_design()}

    monkeypatch.setattr(design, "run_agent", agent)
    budget = Budget(tmp_path / "ledger", 40)
    await design.plan_candidate_design(
        source=source, root=tmp_path, shell=AsyncMock(), budget=budget, model="m"
    )
    no_calls = AsyncMock()
    monkeypatch.setattr(design, "run_agent", no_calls)
    reordered = {"screening": {"a": "é", "b": 2}, "base_sha": "abc", "url": source["url"]}
    resumed = await design.plan_candidate_design(
        source=reordered, root=tmp_path, shell=AsyncMock(), budget=budget, model="m"
    )
    assert resumed.model_dump() == valid_design()
    no_calls.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"url": "https://github.com/org/repo/pull/2", "base_sha": "abc"},
        {"url": "https://github.com/org/repo/pull/1", "base_sha": "different"},
    ],
)
async def test_mismatched_source_fails_before_model_even_if_source_file_overwritten(
    tmp_path, monkeypatch, changed
):
    original = {"url": "https://github.com/org/repo/pull/1", "base_sha": "abc"}
    envelope = {
        "source_digest": hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "design": valid_design(),
    }
    path = tmp_path / "design.json"
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    (tmp_path / "source.json").write_text(json.dumps(changed))
    agent, shell = AsyncMock(), AsyncMock()
    monkeypatch.setattr(design, "run_agent", agent)
    with pytest.raises(ValueError, match="source digest does not match"):
        await design.plan_candidate_design(
            source=changed,
            root=tmp_path,
            shell=shell,
            budget=Budget(tmp_path / "ledger", 40),
            model="m",
        )
    assert path.read_bytes() == before
    agent.assert_not_awaited()
    shell.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contents",
    [
        '{"source_digest":',
        json.dumps({"source_digest": "bad", "design": valid_design()}),
        json.dumps({"source_digest": "0" * 64}),
        json.dumps(valid_design()),
    ],
)
async def test_torn_or_invalid_design_envelope_fails_closed(tmp_path, monkeypatch, contents):
    path = tmp_path / "design.json"
    path.write_text(contents)
    agent, shell = AsyncMock(), AsyncMock()
    monkeypatch.setattr(design, "run_agent", agent)
    with pytest.raises(ValueError):
        await design.plan_candidate_design(
            source={}, root=tmp_path, shell=shell, budget=Budget(tmp_path / "ledger", 40), model="m"
        )
    assert path.read_text() == contents
    agent.assert_not_awaited()
    shell.assert_not_awaited()
