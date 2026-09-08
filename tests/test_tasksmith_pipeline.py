from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite.aio")

from repo2rlenv.tasksmith import pipeline
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.state import ArtifactCorrupt, ReconciliationRequired


def source(number=3969):
    return {
        "id": f"huggingface-accelerate-{number}",
        "repo": "huggingface/accelerate",
        "number": number,
        "url": f"https://github.com/huggingface/accelerate/pull/{number}",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "base_tree_sha": "c" * 40,
        "head_tree_sha": "d" * 40,
    }


@pytest.fixture
def config(tmp_path):
    return TasksmithConfig(
        ledger_path=tmp_path / "ledger.json", ledger_limit_usd=100, campaign_id="fixture"
    )


def fakes(monkeypatch, *, interrupt=False):
    calls = []

    async def discover(*args):
        calls.append("discover")
        return {"artifact": {"fixture": True}}

    async def construct(*args):
        calls.append("construct")
        return {"emitter": {"task_digest": "e" * 64}, "task_path": "never-imported-fixture"}

    async def validate(*args):
        calls.append("validate")
        if interrupt:
            raise RuntimeError("Remote operation unknown; fixture interruption")
        return {"status": "accepted", "repairable": False}

    monkeypatch.setattr(pipeline, "discover", discover)
    monkeypatch.setattr(pipeline, "construct", construct)
    monkeypatch.setitem(
        sys.modules, "repo2rlenv.tasksmith.validation", SimpleNamespace(validate_candidate=validate)
    )
    return calls


def candidate(config, tmp_path):
    return pipeline.Candidate(config, source(), tmp_path / "candidate", tmp_path / "campaign")


def test_completed_graph_resume_does_not_repeat_any_effect(config, tmp_path, monkeypatch):
    calls = fakes(monkeypatch)
    first = asyncio.run(candidate(config, tmp_path).run())
    assert first["status"] == "accepted"
    assert calls == ["discover", "construct", "validate"]
    again = asyncio.run(candidate(config, tmp_path).run())
    assert again == first
    assert calls == ["discover", "construct", "validate"]


def test_pending_effect_requires_reconciliation_no_model_or_remote_retry(
    config, tmp_path, monkeypatch
):
    calls = fakes(monkeypatch, interrupt=True)
    with pytest.raises(RuntimeError, match="operation unknown"):
        asyncio.run(candidate(config, tmp_path).run())
    with pytest.raises(ReconciliationRequired, match="retained uncertain"):
        asyncio.run(candidate(config, tmp_path).run())
    assert calls == ["discover", "construct", "validate"]


def test_resume_after_child_revision_commit_before_graph_checkpoint(config, tmp_path, monkeypatch):
    calls = fakes(monkeypatch)
    original = pipeline.Candidate.construct_node

    async def interrupted(self, state):
        await original(self, state)
        raise RuntimeError("crash after child revision commit before graph checkpoint")

    monkeypatch.setattr(pipeline.Candidate, "construct_node", interrupted)
    with pytest.raises(RuntimeError, match="child revision commit"):
        asyncio.run(candidate(config, tmp_path).run())
    assert calls == ["discover", "construct"]
    monkeypatch.setattr(pipeline.Candidate, "construct_node", original)
    result = asyncio.run(candidate(config, tmp_path).run())
    assert result["status"] == "accepted"
    assert calls == ["discover", "construct", "validate"]


def test_corrupt_completed_effect_never_reused(config, tmp_path, monkeypatch):
    calls = fakes(monkeypatch)
    original = pipeline.Candidate.construct_node

    async def interrupted(self, state):
        await original(self, state)
        raise RuntimeError("checkpoint interrupted")

    monkeypatch.setattr(pipeline.Candidate, "construct_node", interrupted)
    first = candidate(config, tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(first.run())
    operation = next(
        r for r in first.journal.operations(first.pr.key) if r.key.stage == "construction"
    )
    ref = operation.artifacts["result"]
    first.journal.verify_artifact(ref).write_text('{"changed":true}')
    monkeypatch.setattr(pipeline.Candidate, "construct_node", original)
    with pytest.raises(ArtifactCorrupt):
        asyncio.run(candidate(config, tmp_path).run())
    assert calls == ["discover", "construct"]


@pytest.mark.parametrize("changed", ["source", "config"])
def test_candidate_identity_is_immutable_before_effects(config, tmp_path, changed):
    candidate(config, tmp_path)
    before = (tmp_path / "candidate/identity.json").read_bytes()
    new_source = source()
    new_config = config
    if changed == "source":
        new_source["head_sha"] = "f" * 40
    else:
        new_config = config.model_copy(update={"author_model": "different-model"})
    with pytest.raises(ValueError, match="configuration or frozen source changed"):
        pipeline.Candidate(new_config, new_source, tmp_path / "candidate", tmp_path / "campaign")
    assert (tmp_path / "candidate/identity.json").read_bytes() == before


def test_fixed_panel_does_not_drop_failures_or_replace_inputs(config, tmp_path, monkeypatch):
    rows = [source(3850), source(3969)]
    path = tmp_path / "input.json"
    path.write_text(json.dumps(rows))

    async def run(self):
        if self.source["number"] == 3850:
            raise RuntimeError("fixture unresolved")
        return {"status": "accepted", "revision": 0}

    monkeypatch.setattr(pipeline.Candidate, "run", run)
    result = asyncio.run(pipeline.run_panel(config, path, tmp_path / "panel"))
    assert result["inputs"] == 2 and result["accepted"] == 1 and result["remaining_inputs"] == 0
    assert [r["id"] for r in result["results"]] == [r["id"] for r in rows]
    path.write_text(json.dumps([source(3969)]))
    with pytest.raises(ValueError, match="fixed input panel changed"):
        asyncio.run(pipeline.run_panel(config, path, tmp_path / "panel"))


def test_duplicate_pr_is_not_two_panel_slots(tmp_path):
    path = tmp_path / "input.json"
    duplicate = {**source(), "id": "alternate-id-for-the-same-pr"}
    path.write_text(json.dumps([source(), duplicate]))
    with pytest.raises(ValueError, match="Duplicate PR"):
        pipeline.freeze_panel(path, tmp_path / "panel")


@pytest.mark.parametrize("bad_id", ["../outside", "/absolute", "huggingface-accelerate-3850"])
def test_panel_ids_are_safe_and_unique_before_writes(tmp_path, bad_id):
    path = tmp_path / "input.json"
    path.write_text(json.dumps([source(3850), {**source(3969), "id": bad_id}]))
    with pytest.raises(ValueError, match="unique safe"):
        pipeline.freeze_panel(path, tmp_path / "panel")
    assert not (tmp_path / "panel/panel.json").exists()
