from __future__ import annotations

import hashlib
import importlib
import json
import shutil
from pathlib import Path

import pytest

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import CRITERIA, Review, TrialEvidence
from repo2rlenv.curation.review import validate_review_receipt
from repo2rlenv.curation.review_evidence import ReviewEvidenceError

review_module = importlib.import_module("repo2rlenv.curation.review")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def output():
    return Review(
        criteria={
            name: {
                "score": 4,
                "outcome": "pass",
                "explanation": "Controlled receipt verification fixture evidence.",
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


async def finished(tmp_path, monkeypatch):
    root, task = tmp_path / "revision", tmp_path / "revision/task"
    write(task / "instruction.md", "Return the specified α value from the public function.\n")
    write(task / "contract.json", json.dumps({"source_paths": ["src/pkg"]}))
    write(task / "tests/test.py", "assert True\n")
    write(task / "tests/__init__.py", "")
    write(task / "environment/data.bin", "\x00\x01")
    trials = []
    for label in ("baseline", "solver-0-0", "solver-1-0", "adversary"):
        folder = root / "trials" / (label + "-id")
        write(
            folder / "artifacts/manifest.json",
            json.dumps([{"source": "/workspace/src/pkg", "status": "ok"}]),
        )
        write(
            folder / "artifacts/workspace/src/pkg/f.py",
            "value = 1\n" if label.startswith("solver") else "value = 0\n",
        )
        write(
            folder / "agent/trace.jsonl",
            json.dumps(
                {
                    "kind": "model",
                    "message": {"role": "assistant", "content": f"Recorded {label} action"},
                }
            )
            + "\n",
        )
        trials.append(
            TrialEvidence(
                label=label,
                path=str(folder),
                task_digest=digest_task(task),
                reward=1 if label.startswith("solver") else 0,
            )
        )

    async def judge(**kwargs):
        messages = [
            {"role": "system", "content": kwargs["system"]},
            {"role": "user", "content": kwargs["prompt"]},
        ]
        texts = json.loads((root / "review-projections.json").read_text())["texts"]
        for name in json.loads((root / "review-policy.json").read_text())["required_sha256"]:
            if name not in texts:
                texts[name] = (root / name).read_text()
        for i, (name, text) in enumerate(texts.items()):
            assert len(text) < 16000
            arguments = {"path": name, "limit": 16000}
            identity = f"read-{i}"
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": identity,
                            "function": {
                                "name": "read_evidence",
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": identity,
                    "content": await kwargs["handlers"]["read_evidence"](**arguments),
                }
            )
        assert kwargs["validate_final"](output().model_dump_json()) is None
        messages.append({"role": "assistant", "content": output().model_dump_json()})
        return {"messages": messages, "turns": len(texts) + 1, "cost": 0}

    monkeypatch.setattr(review_module, "run_agent", judge)
    await review_module.review(
        task,
        root,
        trials,
        model="mock",
        budget=Budget(tmp_path / "budget", 8),
        acceptance_policy="validity",
    )
    return root, task, trials


@pytest.mark.asyncio
async def test_receipt_survives_real_trial_json_roundtrip_with_default_numeric_cost(
    tmp_path, monkeypatch
):
    root, task, trials = await finished(tmp_path, monkeypatch)
    assert type(trials[0].cost_usd) is int
    raw = json.dumps([trial.model_dump() for trial in trials])
    restored = [TrialEvidence.model_validate(record) for record in json.loads(raw)]
    assert type(restored[0].cost_usd) is float
    assert validate(root, task, restored) == output()
    assert review_module._trial_records(trials) == review_module._trial_records(restored)


def validate(root, task, trials, **kwargs):
    return validate_review_receipt(
        root,
        task,
        trials,
        model=kwargs.get("model", "mock"),
        acceptance_policy=kwargs.get("acceptance_policy", "validity"),
    )


def refresh_bound_hash(root, name):
    receipt_path = root / "review-coverage.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["files_sha256"][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt))


@pytest.mark.asyncio
async def test_valid_receipt_is_read_only_and_binds_exact_review(tmp_path, monkeypatch):
    root, task, trials = await finished(tmp_path, monkeypatch)
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert validate(root, task, trials) == output()
    assert {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    } == before


@pytest.mark.asyncio
@pytest.mark.parametrize("relocate_trial_paths", [False, True])
async def test_published_snapshot_validates_without_raw_exports(
    tmp_path, monkeypatch, relocate_trial_paths
):
    root, _task, trials = await finished(tmp_path, monkeypatch)
    copied = tmp_path / "published/revision"
    shutil.copytree(root, copied, ignore=shutil.ignore_patterns("artifacts"))
    assert not list(copied.glob("trials/*/artifacts"))
    if relocate_trial_paths:
        trials = [
            trial.model_copy(update={"path": str(copied / "trials" / Path(trial.path).name)})
            for trial in trials
        ]
    assert validate(copied, copied / "task", trials) == output()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "stale",
        "task",
        "trace",
        "review",
        "snapshot",
        "trial",
        "model",
        "acceptance_policy",
    ],
)
async def test_missing_stale_or_changed_proof_cannot_be_reused(tmp_path, monkeypatch, change):
    root, task, trials = await finished(tmp_path, monkeypatch)
    kwargs = {}
    if change == "missing":
        (root / "review-coverage.json").unlink()
    elif change == "stale":
        p = root / "review-policy.json"
        d = json.loads(p.read_text())
        d["projection_source_sha256"] = "0" * 64
        p.write_text(json.dumps(d))
        refresh_bound_hash(root, p.name)
    elif change == "task":
        (task / "tests/test.py").write_text("assert False\n")
    elif change == "trace":
        p = Path(trials[2].path) / "agent/trace.jsonl"
        p.write_text(
            p.read_text() + json.dumps({"kind": "unread-action", "content": "changed"}) + "\n"
        )
    elif change == "review":
        p = root / "review.json"
        d = json.loads(p.read_text())
        d["blockers"] = ["a changed verdict"]
        p.write_text(json.dumps(d))
    elif change == "snapshot":
        p = root / "review-submissions.json"
        d = json.loads(p.read_text())
        d["texts"][next(iter(d["texts"]))] += "new text"
        p.write_text(json.dumps(d))
    elif change == "trial":
        trials[2] = trials[2].model_copy(update={"reward": 0})
    else:
        kwargs[change] = "other-model" if change == "model" else "legacy"
    with pytest.raises(ReviewEvidenceError):
        validate(root, task, trials, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fabrication",
    [
        "empty_inventory",
        "subset_projection",
        "empty_reads",
        "unread_verifier",
        "full_fake_reads",
        "invalid_interval",
        "source_subset",
    ],
)
async def test_fabricated_complete_flag_or_subset_cannot_replace_actual_required_reads(
    tmp_path, monkeypatch, fabrication
):
    root, task, trials = await finished(tmp_path, monkeypatch)
    receipt_path = root / "review-coverage.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["complete"], receipt["missing"] = True, {}
    if fabrication == "empty_inventory":
        receipt["required_sha256"], receipt["reads"] = {}, {}
    elif fabrication == "empty_reads":
        receipt["reads"]["review-actions/solver-1-0.txt"] = []
    elif fabrication == "unread_verifier":
        receipt["reads"]["task/tests/test.py"] = []
    elif fabrication == "invalid_interval":
        receipt["reads"]["review-actions/solver-1-0.txt"] = [[0, 10**9]]
    elif fabrication == "subset_projection":
        p = root / "review-projections.json"
        d = json.loads(p.read_text())
        del d["texts"]["review-changes/solver-1-0.diff"]
        p.write_text(json.dumps(d))
        receipt["files_sha256"][p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    elif fabrication == "source_subset":
        p = root / "review-submissions.json"
        d = json.loads(p.read_text())
        del d["submission_inventory"]["trials"]["solver-1-0"]
        p.write_text(json.dumps(d))
        receipt["files_sha256"][p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    else:
        # Keep claimed complete intervals, but remove actual Opus trace read messages.
        p = root / "judge-state.json"
        d = json.loads(p.read_text())
        missing_ids = {
            call["id"]
            for m in d["messages"]
            for call in m.get("tool_calls", [])
            if json.loads(call["function"]["arguments"])["path"] == "review-actions/solver-1-0.txt"
        }
        d["messages"] = [
            m
            for m in d["messages"]
            if m.get("tool_call_id") not in missing_ids
            and not any(c["id"] in missing_ids for c in m.get("tool_calls", []))
        ]
        p.write_text(json.dumps(d))
        receipt["files_sha256"][p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ReviewEvidenceError):
        validate(root, task, trials)


@pytest.mark.asyncio
async def test_changed_review_cannot_be_rebound_without_matching_model_final(tmp_path, monkeypatch):
    root, task, trials = await finished(tmp_path, monkeypatch)
    path = root / "review.json"
    result = json.loads(path.read_text())
    result["blockers"] = ["A replacement verdict"]
    path.write_text(json.dumps(result))
    refresh_bound_hash(root, path.name)
    with pytest.raises(ReviewEvidenceError, match="actual final response"):
        validate(root, task, trials)


@pytest.mark.asyncio
async def test_invalid_read_then_successful_retry_does_not_false_reject(tmp_path, monkeypatch):
    root, task, trials = await finished(tmp_path, monkeypatch)
    path = root / "judge-state.json"
    state = json.loads(path.read_text())
    state["messages"][2:2] = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "invalid-then-corrected",
                    "function": {"name": "read_evidence", "arguments": "{"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "invalid-then-corrected",
            "content": "Tool input error: invalid JSON",
        },
    ]
    state["turns"] += 1
    path.write_text(json.dumps(state))
    refresh_bound_hash(root, path.name)
    assert validate(root, task, trials) == output()
