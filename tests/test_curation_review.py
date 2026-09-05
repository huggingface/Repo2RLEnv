from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.models import CRITERIA, Review, TrialEvidence

review_module = importlib.import_module("repo2rlenv.curation.review")


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


@pytest.fixture
def evidence(tmp_path):
    root = tmp_path.resolve()
    task = root / "task"
    write(task / "contract.json", json.dumps({"source_paths": ["src/pkg"]}))
    write(task / "instruction.md", "Implement the described behavior.")
    trials = []
    for label in ("baseline", "solver-0-0", "adversary"):
        folder = root / "trials" / label
        write(
            folder / "artifacts/manifest.json",
            json.dumps(
                [
                    {
                        "source": "/workspace/src/pkg",
                        "destination": "artifacts/workspace/src/pkg",
                        "status": "ok",
                    }
                ]
            ),
        )
        write(folder / "artifacts/workspace/src/pkg/unchanged.py", "constant = 1\n")
        trials.append(TrialEvidence(label=label, task_digest="digest", reward=0, path=str(folder)))
    return root, task, trials


def source(trial, name):
    return Path(trial.path) / "artifacts/workspace/src/pkg" / name


def good_review():
    return Review.model_validate(
        {
            "criteria": {
                name: {
                    "score": 4,
                    "outcome": "pass",
                    "explanation": "Mock review for testing the evidence reader only.",
                    "evidence": ["task/instruction.md"],
                }
                for name in CRITERIA
            },
            "blockers": [],
            "failure_attribution": {},
            "reward_hacks": [],
            "suggested_repairs": [],
        }
    )


@pytest.mark.asyncio
async def test_reviewer_reads_patches_changed_exports_and_full_trace_pages(evidence, monkeypatch):
    root, task, trials = evidence
    write(task / "solution/solve.sh", "git apply /solution/patch.diff\n")
    write(task / "solution/patch.diff", "--- a/pkg.py\n+++ b/pkg.py\n")
    write(task / "solution/extra.patch", "@@ -1 +1 @@\n-old\n+new\n")
    write(source(trials[0], "changed.py"), "value = 1\n")
    write(source(trials[1], "changed.py"), "value = 2\n")
    write(source(trials[0], "removed.py"), "removed = True\n")
    write(source(trials[1], "added.ts"), "export const value = 3;\n")
    marker = root / "must-not-execute"
    attack = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    write(source(trials[2], "attack.py"), attack)
    trace_text = "α" * 26000 + "middle event\n" + "β" * 26000 + "final event"
    trace = write(Path(trials[1].path) / "agent/trajectory.jsonl", trace_text)
    write(root / "review-submissions.json", '{"old": "stale duplicate text"}')
    write(root / "judge-state.json", '{"old": "stale judge state"}')

    async def judge(**kwargs):
        prompt = kwargs["prompt"]
        read = kwargs["handlers"]["read_evidence"]
        assert str(root) not in prompt
        assert "unchanged.py" not in prompt
        assert "review-submissions.json" not in prompt
        assert "judge-state.json" not in prompt
        assert "stale duplicate text" not in prompt
        assert "git apply" in await read("task/solution/solve.sh")
        assert "--- a/pkg.py" in await read("task/solution/patch.diff")
        assert "+new" in await read("task/solution/extra.patch")
        assert "value = 2" in await read(str(source(trials[1], "changed.py").relative_to(root)))
        assert "value = 1" in await read(str(source(trials[0], "changed.py").relative_to(root)))
        assert "export const" in await read(str(source(trials[1], "added.ts").relative_to(root)))
        assert attack in await read(str(source(trials[2], "attack.py").relative_to(root)))
        assert "middle event" in await read(str(trace.relative_to(root)), offset=25990, limit=40)
        assert "final event" in await read(
            str(trace.relative_to(root)), offset=len(trace_text) - 20
        )
        assert "deleted" in await read("review-evidence.json")
        with pytest.raises(ValueError, match="not a listed"):
            await read(str(source(trials[1], "unchanged.py").relative_to(root)))
        with pytest.raises(ValueError, match="not a listed"):
            await read(str(trace))
        with pytest.raises(ValueError, match="not a listed"):
            await read("review-submissions.json")
        # The persisted and served text must remain the bytes actually reviewed,
        # even if the raw export changes before publication.
        source(trials[1], "changed.py").write_text("changed after indexing")
        return {"messages": [{"content": good_review().model_dump_json()}]}

    monkeypatch.setattr(review_module, "run_agent", judge)
    await review_module.review(
        task, root, trials, model="mock", budget=Budget(root / "budget.json", 1)
    )
    index = json.loads((root / "review-evidence.json").read_text())
    assert any(
        c.get("status") == "deleted" and c["submission"] == "src/pkg/removed.py"
        for c in index["submission_changes"]
    )
    assert not marker.exists()
    snapshot = json.loads((root / "review-submissions.json").read_text())
    changed = str(source(trials[1], "changed.py").relative_to(root))
    assert snapshot["texts"][changed] == "value = 2\n"
    assert snapshot["sha256"][changed] == hashlib.sha256(b"value = 2\n").hexdigest()
    assert not any(name.endswith("unchanged.py") for name in snapshot["texts"])
    state = json.loads((root / "judge-state.json").read_text())
    assert state["messages"][-1]["content"] == good_review().model_dump_json()


def test_source_export_limits_and_binary_files_are_explicit(evidence, monkeypatch):
    root, task, trials = evidence
    monkeypatch.setattr(review_module, "MAX_SOURCE_FILE_BYTES", 128)
    monkeypatch.setattr(review_module, "MAX_CHANGED_BYTES", 20)
    monkeypatch.setattr(review_module, "MAX_CHANGED_FILES", 2)
    write(source(trials[1], "a.py"), "first = 1\n")
    write(source(trials[1], "b.py"), "second = 2\n")
    write(source(trials[1], "binary.dat"), b"\x00binary")
    write(source(trials[1], "large.py"), "x" * 129)
    write(source(trials[1], "z.py"), "last = 3\n")
    skipped = []
    texts, changes = review_module._submitted_evidence(task, root, trials, skipped)
    assert sum(len(t.encode()) for t in texts.values()) <= 20
    assert len(texts) <= 2
    assert any(s["reason"] == "oversized source" and s["bytes"] == 129 for s in skipped)
    assert any(s["reason"] == "changed source catalog limit reached" for s in skipped)
    # Binary detection is checked independently of the aggregate catalog cap.
    monkeypatch.setattr(review_module, "MAX_CHANGED_BYTES", 1000)
    monkeypatch.setattr(review_module, "MAX_CHANGED_FILES", 80)
    skipped = []
    texts, _ = review_module._submitted_evidence(task, root, trials, skipped)
    assert not any(p.endswith("binary.dat") for p in texts)
    assert any(
        s["reason"] == "not stable UTF-8 text" and s["path"].endswith("binary.dat") for s in skipped
    )
    assert any(c.get("status") == "added" for c in changes)


def test_symlinks_special_files_and_incomplete_exports_are_not_treated_as_deletions(
    evidence, tmp_path
):
    root, task, trials = evidence
    secret = write(tmp_path.parent / "outside-review-secret", "private")
    write(source(trials[0], "original.py"), "original = True\n")
    source(trials[1], "alias.py").symlink_to(secret)
    source(trials[1], "directory").symlink_to(secret.parent, target_is_directory=True)
    os.mkfifo(source(trials[1], "pipe"))
    skipped = []
    texts, changes = review_module._submitted_evidence(task, root, trials, skipped)
    assert not any(p.endswith(("alias.py", "pipe")) for p in texts)
    assert any(s["reason"] == "symlink" for s in skipped)
    assert any(s["reason"] == "not regular" for s in skipped)
    assert not any(c.get("status") == "deleted" and c["trial"] == "solver-0-0" for c in changes)


def test_missing_baseline_and_failed_manifest_are_visible(evidence):
    root, task, trials = evidence
    (Path(trials[0].path) / "artifacts/manifest.json").unlink()
    write(source(trials[1], "new.py"), "new = True\n")
    write(
        Path(trials[2].path) / "artifacts/manifest.json",
        '[{"source":"/workspace/src/pkg","status":"failed"}]',
    )
    skipped = []
    _, changes = review_module._submitted_evidence(task, root, trials, skipped)
    assert any(s["reason"] == "export manifest unavailable" for s in skipped)
    assert any(s["reason"] == "export not successful" for s in skipped)
    assert any(
        c.get("submission") == "src/pkg/new.py" and c["status"] == "baseline unavailable"
        for c in changes
    )


def test_scan_limit_explicitly_marks_unenumerated_source(evidence, monkeypatch):
    root, task, trials = evidence
    monkeypatch.setattr(review_module, "MAX_SOURCE_SCAN_FILES", 1)
    write(source(trials[1], "one.py"), "one = True\n")
    write(source(trials[1], "two.py"), "two = True\n")
    skipped = []
    review_module._submitted_evidence(task, root, trials, skipped)
    assert any(s["reason"] == "export scan limit reached" for s in skipped)


def response(content: str, *, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(model_dump=lambda **_: message))],
        usage=SimpleNamespace(model_dump=lambda: {"completion_tokens": 100, "prompt_tokens": 200}),
    )


def retained_state(content: str):
    return {
        "messages": [
            {"role": "system", "content": "Original strict rubric"},
            {"role": "user", "content": "Original evidence catalog and schema"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "read-1",
                        "type": "function",
                        "function": {"name": "read_evidence", "arguments": '{"path":"trace"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "read-1", "content": "Observed verifier bypass"},
            {"role": "assistant", "content": content},
        ],
        "turns": 16,
        "cost": 2.5,
    }


@pytest.mark.asyncio
async def test_valid_final_review_needs_no_model_call(tmp_path, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("A valid review must not make another model request")

    monkeypatch.setattr(review_module, "completion", unexpected)
    result = await review_module.finalize_review(
        retained_state(good_review().model_dump_json()),
        tmp_path / "judge-trace.jsonl",
        "mock",
        Budget(tmp_path / "budget.json", 20),
        [],
        0,
    )
    assert result == good_review()
    assert not (tmp_path / "judge-trace.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", ['{"criteria": {', '{"criteria": {}}', "```"])
async def test_finalization_preserves_evidence_and_uses_remaining_budget_once(
    tmp_path, monkeypatch, incomplete
):
    budget = Budget(tmp_path / "budget.json", 20)
    budget.settle(budget.reserve(3.5, "earlier work and original review"), 3.5)
    state = retained_state(incomplete)
    original = json.loads(json.dumps(state))
    trace = tmp_path / "judge-trace.jsonl"
    tools = [{"type": "function", "function": {"name": "read_evidence"}}]
    expected = good_review().model_copy(deep=True)
    expected.criteria["verifier_integrity"].score = 0
    expected.criteria["verifier_integrity"].outcome = "fail"
    expected.blockers = ["Observed verifier bypass"]
    expected.reward_hacks = ["Submission controls its own reported test results"]
    calls = []

    async def finalize(received_budget, model, messages, **kwargs):
        calls.append(messages)
        assert received_budget is budget
        assert model == "mock"
        assert messages[:-1] == original["messages"]
        assert "100 words" in messages[-1]["content"]
        assert "4 evidence items" in messages[-1]["content"]
        assert "Do not invent evidence, relax the rubric" in messages[-1]["content"]
        assert kwargs == {
            "tools": tools,
            "tool_choice": "none",
            "max_tokens": 16000,
            "max_charge": 5.5,
        }
        budget.settle(budget.reserve(0.2, "finalization"), 0.2)
        return response(expected.model_dump_json()), 0.2

    monkeypatch.setattr(review_module, "completion", finalize)
    result = await review_module.finalize_review(state, trace, "mock", budget, tools, 1)
    assert result == expected
    assert state["turns"] == 17
    assert state["cost"] == pytest.approx(2.7)
    assert state["messages"][:-2] == original["messages"]
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [event["kind"] for event in events] == ["review_finalization", "model"]
    assert events[1]["model"] == "mock"
    assert events[1]["cost_usd"] == 0.2
    assert events[1]["usage"]["completion_tokens"] == 100
    # An interrupted caller can reuse the durable completed response, including
    # when its in-memory state is still the original truncated conversation.
    assert await review_module.finalize_review(original, trace, "mock", budget, tools, 1) == result
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_json", "invalid_schema", "tool_call", "api_error"])
async def test_failed_finalization_is_recorded_and_not_repeated(tmp_path, monkeypatch, failure):
    trace = tmp_path / "judge-trace.jsonl"
    budget = Budget(tmp_path / "budget.json", 20)
    state = retained_state('{"criteria":')
    calls = []

    async def finalize(*args, **kwargs):
        calls.append(kwargs)
        if failure == "api_error":
            raise RuntimeError("Transport failed after request")
        if failure == "invalid_json":
            return response('{"criteria":'), 0.2
        if failure == "invalid_schema":
            return response('{"criteria":{}}'), 0.2
        return response(good_review().model_dump_json(), tool_calls=[{"id": "unexpected"}]), 0.2

    monkeypatch.setattr(review_module, "completion", finalize)
    with pytest.raises((ValueError, RuntimeError)):
        await review_module.finalize_review(state, trace, "mock", budget, [], 0)
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert events[-1]["kind"] == "error"
    assert events[-1]["phase"] == "review_finalization"
    assert events[-1]["model"] == "mock"
    assert events[-1]["error_type"]
    with pytest.raises(ValueError):
        await review_module.finalize_review(state, trace, "mock", budget, [], 0)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_finalization_cannot_extend_original_review_allowance(tmp_path, monkeypatch):
    budget = Budget(tmp_path / "budget.json", 20)
    budget.settle(budget.reserve(8, "original review"), 8)

    async def unexpected(*args, **kwargs):
        pytest.fail("No finalization request is allowed after the review budget is exhausted")

    monkeypatch.setattr(review_module, "completion", unexpected)
    trace = tmp_path / "judge-trace.jsonl"
    with pytest.raises(BudgetExceeded, match="Review cost limit"):
        await review_module.finalize_review(
            retained_state("incomplete"), trace, "mock", budget, [], 0
        )
    event = json.loads(trace.read_text())
    assert event["kind"] == "error"
    assert event["error_type"] == "BudgetExceeded"


@pytest.mark.asyncio
async def test_review_saves_complete_state_before_finalization(evidence, monkeypatch):
    root, task, trials = evidence
    budget = Budget(root / "budget.json", 20)
    state = retained_state('{"criteria":')

    async def judge(**kwargs):
        assert "100 words" in kwargs["prompt"]
        assert "4 evidence items" in kwargs["prompt"]
        budget.settle(budget.reserve(1, "original review"), 1)
        return state

    async def finalize(received_budget, model, messages, **kwargs):
        assert json.loads((root / "judge-state.json").read_text()) == state
        assert messages[:-1] == state["messages"]
        assert kwargs["max_charge"] == 7
        assert kwargs["tools"][0]["function"]["name"] == "read_evidence"
        assert kwargs["tool_choice"] == "none"
        return response(good_review().model_dump_json()), 0.2

    monkeypatch.setattr(review_module, "run_agent", judge)
    monkeypatch.setattr(review_module, "completion", finalize)
    result = await review_module.review(task, root, trials, model="mock", budget=budget)
    assert result == good_review()
    assert Review.model_validate_json((root / "review.json").read_text()) == result
