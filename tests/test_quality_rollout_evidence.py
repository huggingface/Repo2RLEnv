"""Review summaries use captured artifacts, including edits in middle turns."""

import json

import pytest

from repo2rlenv.quality.loop.rollout_evidence import rollout_documents


def test_complete_command_index_and_actual_added_modified_deleted_source(tmp_path):
    task, trial = tmp_path / "task", tmp_path / "trial"
    for parent, name, text in [
        (task, "environment/source/lib/a.py", "old\n"),
        (task, "environment/source/lib/gone.py", "removed\n"),
        (trial, "artifacts/workspace/lib/a.py", "actual\n"),
        (trial, "artifacts/workspace/lib/new.py", "added\n"),
        (
            trial,
            "agent/trajectory.json",
            json.dumps(
                {
                    "steps": [
                        {
                            "step_id": i,
                            "tool_calls": [
                                {
                                    "function_name": "bash_command",
                                    "arguments": {"keystrokes": str(i)},
                                }
                            ],
                        }
                        for i in range(1, 31)
                    ]
                }
            ),
        ),
        (
            trial,
            "artifacts/manifest.json",
            json.dumps(
                [
                    {
                        "source": "/workspace/lib",
                        "destination": "artifacts/workspace/lib",
                        "type": "directory",
                        "status": "ok",
                    }
                ]
            ),
        ),
    ]:
        path = parent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    documents = rollout_documents(task, trial)
    calls = [json.loads(line) for line in documents["tool-calls.jsonl"].splitlines()]
    assert [call["step_id"] for call in calls[1:]] == list(range(1, 31))
    changes = json.loads(documents["submission-index.json"])["changes"]
    assert {row["path"] for row in changes} == {"lib/a.py", "lib/gone.py", "lib/new.py"}
    assert "+actual" in documents["submitted-source.diff"]
    assert "+added" in documents["submitted-source.diff"]
    assert "-removed" in documents["submitted-source.diff"]
    assert next(row for row in changes if row["path"] == "lib/new.py")["before_sha256"] is None


def test_failed_collection_is_not_described_as_source_deletion(tmp_path):
    (tmp_path / "artifacts").mkdir()
    manifest = tmp_path / "artifacts/manifest.json"
    entry = {
        "source": "/workspace/lib/a.py",
        "destination": "artifacts/workspace/lib/a.py",
        "type": "file",
        "status": "failed",
    }
    manifest.write_text(json.dumps([entry]))
    documents = rollout_documents(tmp_path / "task", tmp_path)
    assert "collection_failed" in documents["submission-index.json"]
    assert documents["submitted-source.diff"].startswith("[No captured")
    manifest.write_text(json.dumps([{**entry, "destination": "../outside"}]))
    with pytest.raises(ValueError, match="escapes"):
        rollout_documents(tmp_path / "task", tmp_path)
