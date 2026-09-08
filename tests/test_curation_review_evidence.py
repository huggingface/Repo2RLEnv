from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock

import pytest

from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import CRITERIA, Review, TrialEvidence
from repo2rlenv.curation.review_evidence import (
    POLICY,
    RequiredReads,
    ReviewEvidenceError,
    policy_identity,
    project_trace,
    submission_diff,
)

review = importlib.import_module("repo2rlenv.curation.review")
projection = importlib.import_module("repo2rlenv.curation.review_evidence")


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def final():
    return Review(
        criteria={
            name: {
                "score": 4,
                "outcome": "pass",
                "explanation": "Mock verdict tests delivery of complete evidence.",
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


@pytest.fixture
def setup(tmp_path):
    root, task = tmp_path / "revision", tmp_path / "revision/task"
    write(
        task / "instruction.md", "Remove only a leading wrapper prefix, preserving inner names.\n"
    )
    write(task / "contract.json", json.dumps({"source_paths": ["src/pkg"]}))
    baseline = "# unchanged source context\n" * 4000 + "def normalize(name):\n    return name\n"
    changed = baseline.replace("return name\n", 'return name.replace("_orig_mod.", "")\n')
    trials = []
    for label in ("baseline", "solver-0-0", "solver-1-0", "adversary"):
        folder = root / "trials" / label
        write(
            folder / "artifacts/manifest.json",
            json.dumps([{"source": "/workspace/src/pkg", "status": "ok"}]),
        )
        write(
            folder / "artifacts/workspace/src/pkg/module.py",
            baseline if label in {"baseline", "adversary"} else changed,
        )
        events = [
            {"kind": "input", "prompt": "Task instructions"},
            {
                "kind": "model",
                "turn": 0,
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "edit",
                            "function": {
                                "name": "shell",
                                "arguments": json.dumps({"command": "inspect and edit source"}),
                            },
                        }
                    ],
                },
            },
            {"kind": "tool", "call_id": "edit", "output": "Action completed"},
            {
                "kind": "model",
                "turn": 1,
                "message": {"role": "assistant", "content": "Finished the recorded action."},
            },
        ]
        write(folder / "agent/trace.jsonl", "".join(json.dumps(e) + "\n" for e in events))
        trials.append(
            TrialEvidence(
                label=label,
                path=str(folder),
                task_digest="digest",
                reward=1 if label.startswith("solver") else 0,
            )
        )
    return root, task, trials, Budget(tmp_path / "budget.json", 8)


async def read_required(root, task, kwargs, *, omit=None):
    texts = json.loads((root / "review-projections.json").read_text())["texts"]
    for name in json.loads((root / "review-policy.json").read_text())["required_sha256"]:
        if name not in texts:
            texts[name] = (root / name).read_text()
    read_paths = []
    for name, text in texts.items():
        if name == omit:
            continue
        for offset in range(0, len(text), 16000):
            output = await kwargs["handlers"]["read_evidence"](name, offset=offset)
            assert len(output) < 24000
            read_paths.append(name)
    return read_paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "omit",
    ["review-actions/solver-1-0.txt", "review-changes/solver-1-0.diff", "task/contract.json"],
)
async def test_missing_opus_actions_or_diff_blocks_valid_verdict_and_formatting(
    setup, monkeypatch, omit
):
    root, task, trials, budget = setup

    async def judge(**kwargs):
        await read_required(root, task, kwargs, omit=omit)
        assert omit in kwargs["validate_final"](final().model_dump_json())
        return {
            "messages": [{"role": "assistant", "content": final().model_dump_json()}],
            "turns": 6,
            "cost": 0,
        }

    monkeypatch.setattr(review, "run_agent", judge)
    formatting = AsyncMock(
        side_effect=AssertionError("Incomplete reads must never trigger formatting")
    )
    monkeypatch.setattr(review, "completion", formatting)
    with pytest.raises(ReviewEvidenceError, match="required evidence"):
        await review.review(task, root, trials, model="mock", budget=budget)
    formatting.assert_not_awaited()
    assert not (root / "review.json").exists()
    assert json.loads((root / "review-coverage.json").read_text())["complete"] is False


@pytest.mark.asyncio
async def test_complete_action_and_diff_reads_need_no_full_unchanged_source(setup, monkeypatch):
    root, task, trials, budget = setup

    async def judge(**kwargs):
        assert kwargs["max_turns"] == 16 and kwargs["max_cost"] == 8
        assert kwargs["validate_final"](final().model_dump_json()) is not None
        read_paths = await read_required(root, task, kwargs)
        assert not any("artifacts/workspace" in path for path in read_paths)
        assert kwargs["validate_final"](final().model_dump_json()) is None
        texts = json.loads((root / "review-projections.json").read_text())["texts"]
        diff = texts["review-changes/solver-1-0.diff"]
        assert 'name.replace("_orig_mod.", "")' in diff
        assert len(diff) < 2000
        assert "No submitted source changes" in texts["review-changes/adversary.diff"]
        fullsource = "trials/solver-1-0/artifacts/workspace/src/pkg/module.py"
        assert "unchanged source context" in await kwargs["handlers"]["read_evidence"](fullsource)
        return {
            "messages": [{"role": "assistant", "content": final().model_dump_json()}],
            "turns": 7,
            "cost": 0,
        }

    monkeypatch.setattr(review, "run_agent", judge)
    assert await review.review(task, root, trials, model="mock", budget=budget) == final()
    assert json.loads((root / "review-coverage.json").read_text())["complete"] is True
    identity = json.loads((root / "review-policy.json").read_text())
    assert identity["policy"] == POLICY
    assert identity["inference"]["model"] == "mock"
    assert all(
        identity[name]
        for name in (
            "review_source_sha256",
            "projection_source_sha256",
            "read_tool_sha256",
            "review_schema_sha256",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["tests/test_contract.py", "solution/solve.sh", "task.toml", "environment/Dockerfile"]
)
async def test_verifier_oracle_and_environment_text_cannot_be_skipped(setup, monkeypatch, name):
    root, task, trials, budget = setup
    write(task / name, "# Required task evidence\n")

    async def judge(**kwargs):
        await read_required(root, task, kwargs, omit=f"task/{name}")
        assert f"task/{name}" in kwargs["validate_final"](final().model_dump_json())
        return {
            "messages": [{"role": "assistant", "content": final().model_dump_json()}],
            "turns": 6,
            "cost": 0,
        }

    monkeypatch.setattr(review, "run_agent", judge)
    with pytest.raises(ReviewEvidenceError, match="required evidence"):
        await review.review(task, root, trials, model="mock", budget=budget)


@pytest.mark.asyncio
async def test_empty_package_text_is_covered_and_binary_exclusions_are_explicit(setup, monkeypatch):
    root, task, trials, budget = setup
    write(task / "tests/__init__.py", "")
    write(task / "solution/solve.sh", "#!/bin/sh\nexit 0\n")
    write(task / "environment/data.bin", "\x00\x01")

    async def judge(**kwargs):
        await read_required(root, task, kwargs)
        assert kwargs["validate_final"](final().model_dump_json()) is None
        return {
            "messages": [{"role": "assistant", "content": final().model_dump_json()}],
            "turns": 6,
            "cost": 0,
        }

    monkeypatch.setattr(review, "run_agent", judge)
    assert await review.review(task, root, trials, model="mock", budget=budget) == final()
    receipt = json.loads((root / "review-coverage.json").read_text())
    assert "task/tests/__init__.py" in receipt["required_sha256"]
    assert "task/environment/data.bin" not in receipt["required_sha256"]
    texts = json.loads((root / "review-projections.json").read_text())["texts"]
    inventory = json.loads(texts["review-required-index.json"])[-1]
    assert inventory["excluded_task_data"] == [
        {
            "path": "task/environment/data.bin",
            "reason": "packaged data or unsupported nontext type",
            "bytes": 2,
        }
    ]


@pytest.mark.asyncio
async def test_oversized_verifier_fails_before_model_without_silent_omission(setup, monkeypatch):
    root, task, trials, budget = setup
    write(task / "tests/test_contract.py", "#" * (review.MAX_SOURCE_FILE_BYTES + 1))
    call = AsyncMock()
    monkeypatch.setattr(review, "run_agent", call)
    with pytest.raises(ReviewEvidenceError, match="Oversized required task text"):
        await review.review(task, root, trials, model="mock", budget=budget)
    call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["missing_trace", "malformed_trace", "missing_export", "oversized_projection"]
)
async def test_unavailable_required_evidence_fails_before_model(setup, monkeypatch, failure):
    root, task, trials, budget = setup
    if failure == "missing_trace":
        (root / "trials/solver-1-0/agent/trace.jsonl").unlink()
    elif failure == "malformed_trace":
        (root / "trials/solver-1-0/agent/trace.jsonl").write_text('{"kind":"model"')
    elif failure == "missing_export":
        (root / "trials/adversary/artifacts/manifest.json").unlink()
    else:
        monkeypatch.setattr(projection, "MAX_PROJECTION_CHARS", 10)
    call = AsyncMock()
    monkeypatch.setattr(review, "run_agent", call)
    with pytest.raises(ReviewEvidenceError):
        await review.review(task, root, trials, model="mock", budget=budget)
    call.assert_not_awaited()
    assert json.loads((root / "review-coverage.json").read_text())["complete"] is False


def test_projection_preserves_readable_reasoning_and_arbitrary_tool_fields():
    thinking = [
        {
            "type": "thinking",
            "thinking": "Readable reason for the action",
            "signature": "opaque-secret",
        }
    ]
    events = [
        {
            "kind": "model",
            "message": {
                "role": "assistant",
                "content": "ordinary content",
                "thinking_blocks": thinking,
                "provider_specific_fields": {
                    "thinking_blocks": thinking,
                    "unknown": "retain unknown metadata",
                },
                "tool_calls": [
                    {
                        "id": "x",
                        "function": {
                            "name": "shell",
                            "arguments": '{"signature":"keep user signature","command":"all commands"}',
                        },
                    }
                ],
            },
        },
        {"kind": "tool", "call_id": "x", "output": "all outputs\nsecond line"},
        {"kind": "unknown_future_event", "value": "preserve me"},
    ]
    raw = "".join(json.dumps(e) + "\n" for e in events).encode()
    text, manifest = project_trace(raw, "solver-1")
    for content in (
        "Readable reason",
        "ordinary content",
        "keep user signature",
        "all commands",
        "all outputs",
        "second line",
        "preserve me",
        "retain unknown metadata",
    ):
        assert content in text
    assert "opaque-secret" not in text
    assert text.count("Readable reason") == 1
    assert len(manifest["removed_metadata"]) == 3


@pytest.mark.parametrize(
    "provider", [{}, {"citations": None}, {"thinking_blocks": None}, {"thinking_blocks": []}]
)
def test_nullable_thinking_metadata_does_not_crash_or_remove_meaningful_text(provider):
    event = {
        "kind": "model",
        "message": {
            "role": "assistant",
            "content": "Meaningful final text",
            "thinking_blocks": None,
            "provider_specific_fields": provider,
        },
    }
    projected, _ = project_trace((json.dumps(event) + "\n").encode(), "solver")
    assert "Meaningful final text" in projected


def test_diffs_cover_added_deleted_and_missing_newline_without_marker_pass():
    texts = {"before": "remove", "after": "add"}
    changes = [
        {
            "trial": "s",
            "submission": "deleted",
            "status": "deleted",
            "baseline": "before",
            "evidence": None,
        },
        {
            "trial": "s",
            "submission": "added",
            "status": "added",
            "baseline": None,
            "evidence": "after",
        },
    ]
    text = submission_diff("s", changes, texts, complete=True)
    assert "-remove" in text and "+add" in text and "No newline at end of file" in text
    with pytest.raises(ReviewEvidenceError, match="incomplete"):
        submission_diff("s", [], {}, complete=False)


def test_character_coverage_requires_actual_gap_filling_not_overlapping_pages():
    reads = RequiredReads({"first": "αβγδε", "second": "hi"})
    reads.observe("first", 0, 2)
    reads.observe("first", 1, 2)
    reads.observe("first", 3, 5)
    reads.observe("second", 0, 2)
    assert reads.missing() == {"first": [[2, 3]]}
    reads.observe("first", 2, 3)
    assert reads.feedback() is None


@pytest.mark.asyncio
async def test_full_read_but_turn_exhausted_cannot_add_formatting_call(tmp_path, monkeypatch):
    reads = RequiredReads({"file": "read me"})
    reads.observe("file", 0, 7)
    call = AsyncMock()
    monkeypatch.setattr(review, "completion", call)
    with pytest.raises(ReviewEvidenceError, match="16-turn"):
        await review.finalize_review(
            {"messages": [{"role": "assistant", "content": "{"}], "turns": 16},
            tmp_path / "trace",
            "mock",
            Budget(tmp_path / "budget", 8),
            [],
            0,
            required_reads=reads,
        )
    call.assert_not_awaited()


def test_identity_changes_with_projection_policy_and_inference(monkeypatch):
    before = policy_identity("mock", "validity")
    monkeypatch.setattr(projection, "POLICY", "future")
    assert policy_identity("mock", "validity") != before
    assert policy_identity("other-model", "validity")["inference"] != before["inference"]
