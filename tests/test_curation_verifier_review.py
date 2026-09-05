from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from repo2rlenv.curation.agent import IncompleteModelResponse
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.models import SpecificationReview

verifier = importlib.import_module("repo2rlenv.curation.verifier_review")


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    return path


def feedback(*, passed=True):
    return SpecificationReview(
        score=4 if passed else 2,
        blockers=[] if passed else ["tests/test_contract.py does not observe the gradient"],
        repairs=[] if passed else ["Observe a nondegenerate loss gradient in the protected test"],
        evidence=["instruction.md requires learning; tests/test_contract.py only checks values"],
    )


def state(result=None, *, content=None, **message):
    return {
        "messages": [
            {
                "role": "assistant",
                "content": content
                if content is not None
                else (result or feedback()).model_dump_json(),
                **message,
            }
        ],
        "turns": 2,
        "cost": 0.15,
    }


async def read_all(kwargs, *, skip=None):
    names = kwargs["tools"][0]["function"]["parameters"]["properties"]["path"]["enum"]
    read = kwargs["handlers"]["read_evidence"]
    for name in names:
        if name == skip:
            continue
        offset = 0
        while True:
            page = await read(name, offset=offset)
            span, size = page.partition("\n")[0].split("characters ")[1].split(" of ")
            offset = int(span.split(":")[1])
            if offset == int(size):
                break


@pytest.fixture
def setup(tmp_path, monkeypatch):
    task, root = tmp_path / "task", tmp_path / "candidate"
    write(task / "instruction.md", "The update must be differentiable and return the input sum.")
    write(task / "contract.json", '{"requirements": [{"tests": ["test_sum"]}]}')
    write(task / "tests/test_contract.py", "def test_sum():\n    assert 1 + 1 == 2\n")
    write(task / "tests/helpers/data.py", "DATA = [1, 2, 3]\n")
    write(task / "tests/fixture.json", '{"value": 3}')
    write(task / "tests/__init__.py", "")
    write(task / "solution/solve.sh", "#!/bin/sh\nexit 0\n")
    write(task / "solution/patch.diff", "--- a/module.py\n+++ b/module.py\n")
    write(task / "environment/Dockerfile", "FROM python:3.12-slim\n")
    write(task / "task.toml", "[environment]\ncpus = 2\n")
    budget = Budget(tmp_path / "budget.json", 20)

    async def judge(**kwargs):
        await read_all(kwargs)
        reservation = kwargs["budget"].reserve(0.2, "mock-verifier-review")
        kwargs["budget"].settle(reservation, 0.15)
        return state()

    agent = AsyncMock(side_effect=judge)
    monkeypatch.setattr(verifier, "run_agent", agent)
    return SimpleNamespace(task=task, root=root, budget=budget, agent=agent)


async def review(s, *, model="anthropic/claude-opus-5"):
    return await verifier.review_verifier(s.task, s.root, model=model, budget=s.budget)


def record_path(s):
    return next((s.root / "verifier-reviews").glob("*/result.json"))


@pytest.mark.asyncio
async def test_bounded_review_reads_all_files_and_reuses_durable_cache(setup):
    s = setup
    assert (await review(s)).passed
    assert (await review(s)).passed
    s.agent.assert_awaited_once()
    call = s.agent.call_args.kwargs
    assert call["model"] == "anthropic/claude-opus-5"
    assert call["budget"] is s.budget
    assert call["max_turns"] == 10
    assert call["max_cost"] == 2
    assert list(call["handlers"]) == ["read_evidence"]
    assert [t["function"]["name"] for t in call["tools"]] == ["read_evidence"]
    record = json.loads(record_path(s).read_text())
    assert record["status"] == "completed"
    assert record["cost_usd"] == record["charged_usd"] == 0.15
    assert record["identity"]["limits"]["input_bytes"] == 128_000
    assert set(record["reads"]) == {
        p.relative_to(s.task).as_posix() for p in s.task.rglob("*") if p.is_file()
    }
    assert all(
        record_path(s).with_name(n).is_file() for n in ("input.json", "state.json", "trace.jsonl")
    )
    assert not list(s.task.rglob("*review*"))


@pytest.mark.asyncio
async def test_cache_identity_covers_tests_helpers_gold_resources_model_and_policy(
    setup, monkeypatch
):
    s = setup
    await review(s)
    for index, name in enumerate(
        [
            "instruction.md",
            "tests/helpers/data.py",
            "tests/fixture.json",
            "solution/patch.diff",
            "task.toml",
        ],
        2,
    ):
        with (s.task / name).open("a") as stream:
            stream.write("\n# changed\n")
        await review(s)
        assert s.agent.await_count == index
    await review(s, model="anthropic/claude-opus-4-6")
    assert s.agent.await_count == 7
    monkeypatch.setattr(verifier, "SYSTEM", verifier.SYSTEM + "\nUpdated verifier policy.")
    await review(s)
    assert s.agent.await_count == 8


@pytest.mark.asyncio
async def test_long_unicode_files_are_read_completely_without_executing_evidence(setup):
    s = setup
    marker = s.root / "must-not-execute"
    text = f"raise RuntimeError({str(marker)!r})\n# " + "β" * 25_000
    write(s.task / "tests/helpers/data.py", text)
    assert (await review(s)).passed
    saved = json.loads(record_path(s).with_name("input.json").read_text())
    assert saved["texts"]["tests/helpers/data.py"] == text
    record = json.loads(record_path(s).read_text())
    assert len(record["reads"]["tests/helpers/data.py"]) == 2
    assert not marker.exists()


@pytest.mark.asyncio
async def test_evidence_tool_is_allowlisted_bounded_and_rejects_invalid_offsets(setup):
    s = setup
    await review(s)
    read = s.agent.call_args.kwargs["handlers"]["read_evidence"]
    for path in ("../secret", str(s.task / "instruction.md"), "solution/missing.py", None):
        with pytest.raises(ValueError, match="not a listed"):
            await read(path)
    for arguments in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": "100"}):
        with pytest.raises(ValueError, match="integers"):
            await read("instruction.md", **arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["unread", "gap", "malformed", "missing", "tool_calls", "wrong_role"]
)
async def test_incomplete_reviews_fail_closed_and_never_repeat_calls(setup, failure):
    s = setup

    async def judge(**kwargs):
        await read_all(
            kwargs, skip="tests/helpers/data.py" if failure in {"unread", "gap"} else None
        )
        if failure == "gap":
            await kwargs["handlers"]["read_evidence"]("tests/helpers/data.py", offset=1)
        if failure == "missing":
            return {"messages": [], "cost": 0, "turns": 1}
        if failure == "malformed":
            return state(content="{")
        if failure == "tool_calls":
            return state(tool_calls=[{"id": "pending"}])
        if failure == "wrong_role":
            return state(role="user")
        return state()

    s.agent.side_effect = judge
    with pytest.raises(verifier.VerifierReviewError, match="Incomplete verifier review"):
        await review(s)
    with pytest.raises(verifier.VerifierReviewError, match="Cached verifier review unavailable"):
        await review(s)
    assert s.agent.await_count == 1
    assert json.loads(record_path(s).read_text())["status"] == "error"


@pytest.mark.asyncio
async def test_evidence_changed_during_review_is_frozen_but_cannot_pass(setup):
    s = setup
    original = (s.task / "tests/helpers/data.py").read_text()

    async def judge(**kwargs):
        write(s.task / "tests/helpers/data.py", "DATA = [999]\n")
        assert original in await kwargs["handlers"]["read_evidence"]("tests/helpers/data.py")
        await read_all(kwargs)
        return state()

    s.agent.side_effect = judge
    with pytest.raises(verifier.VerifierReviewError, match="changed during"):
        await review(s)
    saved = json.loads(record_path(s).with_name("input.json").read_text())
    assert saved["texts"]["tests/helpers/data.py"] == original
    assert json.loads(record_path(s).read_text())["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["truncated", "budget", "cancelled"])
async def test_failure_and_cancellation_remain_durable_and_charged(setup, failure):
    s = setup
    error = {
        "truncated": IncompleteModelResponse("max_tokens", state(content="{")),
        "budget": BudgetExceeded("cap reached"),
        "cancelled": asyncio.CancelledError(),
    }[failure]

    async def judge(**kwargs):
        await kwargs["handlers"]["read_evidence"]("instruction.md")
        kwargs["budget"].reserve(0.2, "unfinished-review")
        raise error

    s.agent.side_effect = judge
    with pytest.raises(type(error)):
        await review(s)
    record = json.loads(record_path(s).read_text())
    assert record["status"] == "error"
    assert record["error_type"] == type(error).__name__
    assert record["charged_usd"] == pytest.approx(0.2)
    assert record["reads"]["instruction.md"]
    if failure == "truncated":
        assert record_path(s).with_name("state.json").is_file()
    with pytest.raises(verifier.VerifierReviewError):
        await review(s)
    assert s.agent.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "empty",
        "binary",
        "nul",
        "symlink",
        "fifo",
        "file_limit",
        "total_limit",
        "too_many",
        "contract",
    ],
)
async def test_incomplete_or_oversized_evidence_never_calls_model(setup, failure):
    s = setup
    path = s.task / "tests/test_contract.py"
    if failure == "missing":
        path.unlink()
    elif failure == "empty":
        path.write_text(" ")
    elif failure == "binary":
        path.write_bytes(b"\xff")
    elif failure == "nul":
        path.write_bytes(b"\0")
    elif failure == "symlink":
        path.unlink()
        path.symlink_to(s.task / "instruction.md")
    elif failure == "fifo":
        os.mkfifo(s.task / "tests/pipe.py")
    elif failure == "file_limit":
        path.write_text("x" * (verifier.MAX_FILE_BYTES + 1))
    elif failure == "total_limit":
        for i in range(3):
            write(s.task / f"tests/helper{i}.py", "x" * 43_000)
    elif failure == "too_many":
        for i in range(verifier.MAX_FILES + 1):
            write(s.task / f"tests/helper{i}.py", "x")
    elif failure == "contract":
        (s.task / "contract.json").write_text("[]")
    with pytest.raises(verifier.VerifierReviewError, match="Cannot review verifier evidence"):
        await review(s)
    s.agent.assert_not_awaited()
    assert (
        json.loads((s.root / "verifier-reviews/input-error.json").read_text())["status"] == "error"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["task", "tests", "nested", "solution", "ancestor"])
async def test_symlinked_evidence_directories_are_rejected(setup, tmp_path, target):
    s = setup
    if target == "task":
        alias = tmp_path / "task-alias"
        alias.symlink_to(s.task, target_is_directory=True)
        s.task = alias
    elif target == "ancestor":
        alias = tmp_path / "parent-alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        s.task = alias / "task"
    elif target == "nested":
        (s.task / "tests/linked").symlink_to(s.task / "solution", target_is_directory=True)
    else:
        path = s.task / target
        moved = tmp_path / f"original-{target}"
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    with pytest.raises(verifier.VerifierReviewError):
        await review(s)
    s.agent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["reads", "input", "missing_state", "state", "trace", "symlink"])
async def test_damaged_completed_cache_never_passes_or_repeats_a_call(setup, damage):
    s = setup
    await review(s)
    result = record_path(s)
    if damage == "reads":
        value = json.loads(result.read_text())
        value["reads"]["instruction.md"] = [[0, 10**9]]
        result.write_text(json.dumps(value))
    elif damage == "input":
        result.with_name("input.json").write_text("{}")
    elif damage == "missing_state":
        result.with_name("state.json").unlink()
    elif damage == "state":
        result.with_name("state.json").write_text("{")
    elif damage == "trace":
        result.with_name("trace.jsonl").write_text('{"kind": "incomplete"}\n')
    elif damage == "symlink":
        result.unlink()
        result.symlink_to(result.with_name("input.json"))
    with pytest.raises(verifier.VerifierReviewError, match="Cached verifier review unavailable"):
        await review(s)
    assert s.agent.await_count == 1


@pytest.mark.asyncio
async def test_failed_quality_feedback_is_cached_without_fishing_for_a_pass(setup):
    s = setup
    failed = feedback(passed=False)

    async def judge(**kwargs):
        await read_all(kwargs)
        return state(failed)

    s.agent.side_effect = judge
    assert await review(s) == failed
    assert await review(s) == failed
    assert s.agent.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_review_does_not_duplicate_the_paid_attempt(setup):
    s = setup
    started, finish = asyncio.Event(), asyncio.Event()

    async def judge(**kwargs):
        started.set()
        await finish.wait()
        await read_all(kwargs)
        return state()

    s.agent.side_effect = judge
    first = asyncio.create_task(review(s))
    await started.wait()
    try:
        with pytest.raises(
            verifier.VerifierReviewError, match="Cached verifier review unavailable"
        ):
            await review(s)
    finally:
        finish.set()
    assert (await first).passed
    assert s.agent.await_count == 1
