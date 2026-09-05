from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

from repo2rlenv.curation.budget import Budget
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

    async def judge(**kwargs):
        prompt = kwargs["prompt"]
        read = kwargs["handlers"]["read_evidence"]
        assert str(root) not in prompt
        assert "unchanged.py" not in prompt
        assert "review-submissions.json" not in prompt
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
