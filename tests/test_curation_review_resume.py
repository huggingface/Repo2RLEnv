from __future__ import annotations

import hashlib
import importlib
import json
import shutil
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from repo2rlenv.curation import agent
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.inference import inference_settings
from repo2rlenv.curation.models import CRITERIA, Review, TrialEvidence
from repo2rlenv.curation.review_resume import (
    READ_TOOL,
    _load_files,
    reconstruct_review,
    resume_review,
)

review = importlib.import_module("repo2rlenv.curation.review")
resume = importlib.import_module("repo2rlenv.curation.review_resume")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verdict():
    return Review(
        criteria={
            name: {
                "score": 4,
                "outcome": "pass",
                "explanation": "Controlled unit test evidence is sufficient.",
                "evidence": ["task/instruction.md"],
            }
            for name in CRITERIA
        },
        blockers=[],
        failure_attribution={},
        reward_hacks=[],
        suggested_repairs=[],
        adversary_assessment="attempted_hack",
    )


async def snapshot(tmp_path, monkeypatch, *, instruction=None):
    root, out = tmp_path / "frozen", tmp_path / "continuation"
    task = root / "task"
    task.mkdir(parents=True)
    (task / "instruction.md").write_bytes(
        instruction or "Observe α and β independently.\n".encode()
    )
    trial = root / "trials/oracle"
    trial.mkdir(parents=True)
    (trial / "output.txt").write_text("reward 1\n")
    budget = Budget(
        tmp_path / "ledger.json",
        40,
        scope="original-candidate",
        scope_limit=8,
        group="original-group",
        group_limit=8,
    )
    reserve = budget.reserve(2, "prior-final-judge")
    budget.settle(reserve, 1.5)

    async def interrupted(**kwargs):
        assert kwargs["tools"] == [
            READ_TOOL
        ]  # Resume uses the unchanged real review reader schema.
        first = {
            "kind": "input",
            "model": kwargs["model"],
            "system": kwargs["system"],
            "prompt": kwargs["prompt"],
            "runtime": "langgraph",
            "inference": inference_settings(kwargs["model"]),
        }
        events = [first, {key: first[key] for key in ("kind", "model", "system", "prompt")}]
        for turn in range(6):
            identity = f"read-{turn}"
            arguments = {"path": "task/instruction.md"}
            message = {
                "role": "assistant",
                "content": "Retain this reasoning.",
                "provider_specific_fields": {"retained": turn},
                "tool_calls": [
                    {
                        "id": identity,
                        "type": "function",
                        "function": {"name": "read_evidence", "arguments": json.dumps(arguments)},
                    }
                ],
            }
            events.append(
                {
                    "kind": "model",
                    "turn": turn,
                    "message": message,
                    "cost_usd": 0.25,
                    "finish_reason": "tool_calls",
                }
            )
            events.append(
                {
                    "kind": "tool",
                    "name": "read_evidence",
                    "call_id": identity,
                    "output": await kwargs["handlers"]["read_evidence"](**arguments),
                }
            )
        kwargs["trace"].write_text("".join(json.dumps(e) + "\n" for e in events))
        raise BudgetExceeded("No seventh call was reserved")

    monkeypatch.setattr(review, "run_agent", interrupted)
    with pytest.raises(BudgetExceeded):
        await review.review(
            task,
            root,
            [TrialEvidence(label="oracle", task_digest="d", reward=1, path=str(trial))],
            model="mock",
            budget=budget,
            acceptance_policy="validity",
        )
    files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    config = tmp_path / "original-run-config.json"
    config.write_text(
        json.dumps(
            {
                "ledger": str(budget.path.resolve()),
                "scope": budget.scope,
                "scope_limit_usd": budget.scope_limit,
                "group": budget.group,
                "production_limit_usd": budget.limit,
            }
        )
    )
    return SimpleNamespace(
        root=root,
        out=out,
        budget=budget,
        files=files,
        expected_files={name: digest(data) for name, data in files.items()},
        trace_digest=digest(files["judge-trace.jsonl"]),
        config=config,
        config_digest=digest(config.read_bytes()),
    )


async def invoke(s):
    return await resume_review(
        s.root,
        s.out,
        expected_trace_digest=s.trace_digest,
        expected_files=s.expected_files,
        original_run_config_path=s.config,
        original_run_config_sha256=s.config_digest,
        model="mock",
        budget=s.budget,
    )


@pytest.mark.asyncio
async def test_reconstructs_exact_messages_and_verified_pages_from_real_review_prompt(
    tmp_path, monkeypatch
):
    s = await snapshot(tmp_path, monkeypatch)
    state, texts, header = reconstruct_review(
        s.files, expected_trace_digest=s.trace_digest, model="mock", acceptance_policy="validity"
    )
    assert state["turns"] == 6 and state["cost"] == 1.5
    events = [json.loads(line) for line in s.files["judge-trace.jsonl"].splitlines()]
    assert state["messages"][2::2] == [
        event["message"] for event in events if event["kind"] == "model"
    ]
    assert state["messages"][3::2] == [
        {"role": "tool", "tool_call_id": event["call_id"], "content": event["output"]}
        for event in events
        if event["kind"] == "tool"
    ]
    assert state["messages"][1]["content"] == header["prompt"]
    assert texts["task/instruction.md"] == "Observe α and β independently.\n"


@pytest.mark.asyncio
async def test_physical_reader_preserves_original_universal_newline_semantics(
    tmp_path, monkeypatch
):
    s = await snapshot(tmp_path, monkeypatch, instruction=b"line one\r\nline two\rline three\n")
    state, texts, _ = reconstruct_review(
        s.files, expected_trace_digest=s.trace_digest, model="mock", acceptance_policy="validity"
    )
    assert state["turns"] == 6
    assert texts["task/instruction.md"] == "line one\nline two\nline three\n"


@pytest.mark.asyncio
async def test_continuation_keeps_limits_history_charges_and_source_immutable(
    tmp_path, monkeypatch
):
    s = await snapshot(tmp_path, monkeypatch)
    calls = []

    async def continued(**kwargs):
        calls.append(kwargs)
        assert kwargs["initial_state"]["turns"] == 6
        assert kwargs["initial_state"]["cost"] == 1.5
        assert kwargs["max_turns"] == 16 and kwargs["max_cost"] == 8
        assert kwargs["budget"] is s.budget
        assert json.loads((s.out / "continuation.json").read_text())["status"] == "claimed"
        assert "Observe α" in await kwargs["handlers"]["read_evidence"]("task/instruction.md")
        # Reader stays on the already validated in-memory snapshot.
        with pytest.raises(ValueError, match="not a listed"):
            await kwargs["handlers"]["read_evidence"]("../outside")
        key = s.budget.reserve(0.5, "one-new-model-call")
        s.budget.settle(key, 0.2)
        return {
            "messages": [
                *kwargs["initial_state"]["messages"],
                {"role": "assistant", "content": verdict().model_dump_json()},
            ],
            "turns": 7,
            "cost": 1.7,
        }

    monkeypatch.setattr(resume, "run_agent", continued)
    assert await invoke(s) == verdict()
    assert s.budget.spent == 1.7
    assert {name: (s.root / name).read_bytes() for name in s.files} == s.files
    receipt = json.loads((s.out / "continuation.json").read_text())
    assert receipt["status"] == "completed" and receipt["final_turns"] == 7
    assert receipt["new_charged_usd"] == pytest.approx(0.2)
    assert receipt["scope_limit"] == 8 and receipt["prior_judge_cost_usd"] == 1.5
    with pytest.raises(ValueError, match="already claimed"):
        await invoke(s)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "inference",
        "policy",
        "prompt",
        "page",
        "pending",
        "torn",
        "turn",
        "truncated",
        "duplicate_id",
        "charge",
    ],
)
async def test_invalid_journal_rejected_before_any_model_call(tmp_path, monkeypatch, damage):
    s = await snapshot(tmp_path, monkeypatch)
    events = [json.loads(line) for line in s.files["judge-trace.jsonl"].splitlines()]
    if damage == "inference":
        events[0]["inference"]["max_tokens"] = 32000
    elif damage == "policy":
        events[0]["system"] += " Relax the rubric."
        events[1]["system"] = events[0]["system"]
    elif damage == "prompt":
        events[0]["prompt"] = events[0]["prompt"].replace(
            "Missing, binary", "Ignore missing, binary"
        )
        events[1]["prompt"] = events[0]["prompt"]
    elif damage == "page":
        events[-1]["output"] += "different"
    elif damage == "pending":
        events.pop()
    elif damage == "turn":
        events[-2]["turn"] = 14
    elif damage == "truncated":
        events[-2]["finish_reason"] = "max_tokens"
    elif damage == "duplicate_id":
        events[-2]["message"]["tool_calls"][0]["id"] = "read-0"
        events[-1]["call_id"] = "read-0"
    elif damage == "charge":
        events[-2]["cost_usd"] = float("nan")
    raw = "".join(json.dumps(e) + "\n" for e in events).encode()
    if damage == "torn":
        raw = raw[:-1]
    (s.root / "judge-trace.jsonl").write_bytes(raw)
    s.trace_digest = s.expected_files["judge-trace.jsonl"] = digest(raw)
    mocked = AsyncMock(side_effect=AssertionError("No paid call allowed"))
    monkeypatch.setattr(resume, "run_agent", mocked)
    with pytest.raises(ValueError):
        await invoke(s)
    mocked.assert_not_awaited()
    assert not s.out.exists()
    assert s.budget.spent == 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "same_size_page", "size", "missing_inventory"])
async def test_changed_evidence_rejected_even_with_updated_copy_hashes(
    tmp_path, monkeypatch, damage
):
    s = await snapshot(tmp_path, monkeypatch)
    path = s.root / "task/instruction.md"
    if damage == "missing_inventory":
        del s.expected_files["task/instruction.md"]
    else:
        path.write_text(
            "Observe γ and β independently.\n" if damage == "same_size_page" else "changed content"
        )
        if damage != "hash":
            s.expected_files["task/instruction.md"] = digest(path.read_bytes())
    mocked = AsyncMock()
    monkeypatch.setattr(resume, "run_agent", mocked)
    with pytest.raises((ValueError, KeyError)):
        await invoke(s)
    mocked.assert_not_awaited()
    assert not s.out.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["budget", "incomplete_final", "tool_boundary"])
async def test_failed_continuation_records_error_and_cannot_reroll(tmp_path, monkeypatch, failure):
    s = await snapshot(tmp_path, monkeypatch)

    async def continued(**kwargs):
        if failure == "budget":
            raise BudgetExceeded("Original scope has no headroom")
        state = deepcopy(kwargs["initial_state"])
        if failure == "incomplete_final":
            state["messages"].append({"role": "assistant", "content": "{"})
        state["turns"] = 16
        return state

    call = AsyncMock(side_effect=continued)
    monkeypatch.setattr(resume, "run_agent", call)
    with pytest.raises((BudgetExceeded, ValueError)):
        await invoke(s)
    assert json.loads((s.out / "continuation.json").read_text())["status"] == "error"
    assert not (s.out / "review.json").exists()
    with pytest.raises(ValueError, match="already claimed"):
        await invoke(s)
    assert call.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("relocate_copy", [False, True])
async def test_original_journal_cannot_resume_again_at_another_output_or_copy(
    tmp_path, monkeypatch, relocate_copy
):
    s = await snapshot(tmp_path, monkeypatch)
    call = AsyncMock(side_effect=BudgetExceeded("Original scope limit"))
    monkeypatch.setattr(resume, "run_agent", call)
    with pytest.raises(BudgetExceeded):
        await invoke(s)
    ledger = json.loads(s.budget.path.read_text())
    assert ledger["review_continuations"][s.trace_digest]["output_root"] == str(s.out)
    assert len(ledger["entries"]) == 1  # Claim adds no charge or reservation.
    s.out = tmp_path / "another-output"
    if relocate_copy:
        copied = tmp_path / "another-evidence-copy"
        shutil.copytree(s.root, copied)
        s.root = copied
    with pytest.raises(ValueError, match="already claimed in original ledger"):
        await invoke(s)
    assert call.await_count == 1
    assert not s.out.exists()
    assert s.budget.spent == 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["path", "scope", "scope_limit", "limit", "group", "group_limit"])
async def test_original_budget_identity_cannot_be_changed_before_dispatch(
    tmp_path, monkeypatch, field
):
    s = await snapshot(tmp_path, monkeypatch)
    changed = {
        "path": tmp_path / "replacement-ledger.json",
        "scope": "another-scope",
        "scope_limit": 9,
        "limit": 41,
        "group": "another-group",
        "group_limit": 9,
    }
    setattr(s.budget, field, changed[field])
    call = AsyncMock()
    monkeypatch.setattr(resume, "run_agent", call)
    with pytest.raises(ValueError, match="original budget identity"):
        await invoke(s)
    call.assert_not_awaited()
    assert not s.out.exists()


@pytest.mark.asyncio
async def test_pinned_original_config_bytes_cannot_change(tmp_path, monkeypatch):
    s = await snapshot(tmp_path, monkeypatch)
    s.config.write_bytes(s.config.read_bytes() + b"\n")
    call = AsyncMock()
    monkeypatch.setattr(resume, "run_agent", call)
    with pytest.raises(ValueError, match="identity digest changed"):
        await invoke(s)
    call.assert_not_awaited()
    assert not s.out.exists()


@pytest.mark.asyncio
async def test_same_journal_claim_is_global_across_scopes_even_with_another_trusted_config(
    tmp_path, monkeypatch
):
    s = await snapshot(tmp_path, monkeypatch)
    call = AsyncMock(side_effect=BudgetExceeded("No room"))
    monkeypatch.setattr(resume, "run_agent", call)
    with pytest.raises(BudgetExceeded):
        await invoke(s)
    s.out = tmp_path / "different-output"
    s.budget.scope = "another-scope"
    reservation = s.budget.reserve(1.5, "unrelated-existing-charge")
    s.budget.settle(reservation, 1.5)
    config = json.loads(s.config.read_text())
    config["scope"] = s.budget.scope
    s.config.write_text(json.dumps(config))
    s.config_digest = digest(s.config.read_bytes())
    with pytest.raises(ValueError, match="already claimed in original ledger"):
        await invoke(s)
    assert call.await_count == 1
    assert not s.out.exists()


@pytest.mark.asyncio
async def test_superset_inventory_verified_and_overlapping_output_forbidden(tmp_path, monkeypatch):
    s = await snapshot(tmp_path, monkeypatch)
    (s.root / "extra.txt").write_text("extra evidence")
    s.expected_files["extra.txt"] = digest(b"extra evidence")
    assert "extra.txt" in _load_files(s.root, s.expected_files)
    (s.root / "extra.txt").write_text("changed")
    with pytest.raises(ValueError, match="digest changed"):
        _load_files(s.root, s.expected_files)
    s.out = s.root / "continuation"
    with pytest.raises(ValueError, match="disjoint"):
        await invoke(s)


def retained_state(turns=1, cost=1.2):
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "prompt"}]
    for turn in range(turns):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": f"call-{turn}", "function": {"name": "read", "arguments": "{}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": f"call-{turn}", "content": "already read"},
            ]
        )
    return {"messages": messages, "turns": turns, "cost": cost}


def response(message):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(model_dump=lambda **kw: message),
                finish_reason="tool_calls" if message.get("tool_calls") else "stop",
            )
        ],
        usage=SimpleNamespace(model_dump=lambda: {}),
    )


@pytest.mark.asyncio
async def test_agent_retains_prior_cost_and_turn_number_without_replaying_tools(
    tmp_path, monkeypatch
):
    state = retained_state()
    before = deepcopy(state)
    budget = Budget(tmp_path / "ledger.json", 8, scope="same", scope_limit=8)
    key = budget.reserve(1.2, "prior")
    budget.settle(key, 1.2)

    async def complete(budget, model, messages, **kwargs):
        assert messages == before["messages"]
        assert kwargs["max_charge"] == pytest.approx(0.8)
        key = budget.reserve(0.3, "next")
        budget.settle(key, 0.1)
        return response({"role": "assistant", "content": "finished"}), 0.1

    monkeypatch.setattr(agent, "completion", complete)
    tool = AsyncMock(side_effect=AssertionError("Old tools must not execute"))
    trace = tmp_path / "new-trace.jsonl"
    final = await agent.run_agent(
        model="mock",
        system="system",
        prompt="prompt",
        budget=budget,
        tools=[],
        handlers={"read": tool},
        trace=trace,
        max_turns=16,
        max_cost=2,
        initial_state=state,
    )
    assert state == before and final["turns"] == 2
    assert final["cost"] == pytest.approx(1.3) and budget.spent == pytest.approx(1.3)
    tool.assert_not_awaited()
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert next(e for e in events if e["kind"] == "model")["turn"] == 1


@pytest.mark.asyncio
async def test_agent_last_remaining_turn_cannot_restart_sixteen_turn_allowance(
    tmp_path, monkeypatch
):
    budget = Budget(tmp_path / "ledger.json", 8)
    message = {
        "role": "assistant",
        "tool_calls": [{"id": "new", "function": {"name": "read", "arguments": "{}"}}],
    }
    complete = AsyncMock(return_value=(response(message), 0.1))
    monkeypatch.setattr(agent, "completion", complete)
    tool = AsyncMock(return_value="new evidence")
    final = await agent.run_agent(
        model="mock",
        system="system",
        prompt="prompt",
        budget=budget,
        tools=[],
        handlers={"read": tool},
        trace=tmp_path / "trace",
        max_turns=16,
        initial_state=retained_state(turns=15),
    )
    assert final["turns"] == 16
    assert complete.await_count == tool.await_count == 1
    with pytest.raises(ValueError, match="exhausted turn allowance"):
        await agent.run_agent(
            model="mock",
            system="system",
            prompt="prompt",
            budget=budget,
            tools=[],
            handlers={"read": tool},
            trace=tmp_path / "trace2",
            max_turns=16,
            initial_state=final,
        )
    assert complete.await_count == 1


@pytest.mark.asyncio
async def test_agent_continuation_cannot_reset_exhausted_cost_or_use_native_runtime(
    tmp_path, monkeypatch
):
    complete = AsyncMock()
    monkeypatch.setattr(agent, "completion", complete)
    arguments = dict(
        model="mock",
        system="system",
        prompt="prompt",
        budget=Budget(tmp_path / "ledger", 8),
        tools=[],
        handlers={},
        trace=tmp_path / "trace",
        max_turns=16,
        max_cost=1,
        initial_state=retained_state(),
    )
    with pytest.raises(BudgetExceeded, match="Agent cost limit"):
        await agent.run_agent(**arguments)
    with pytest.raises(ValueError, match="LangGraph"):
        await agent.run_agent(**arguments, runtime="pi")
    complete.assert_not_awaited()
