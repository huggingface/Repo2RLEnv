"""Prepared counterexample definitions remain bound and frozen without old rewards."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.loop.artifacts import digest, task_identity
from repo2rlenv.quality.loop.models import LoopResult, ProbeManifest
from repo2rlenv.tasksmith import batch
from repo2rlenv.tasksmith import runner as runner_module
from repo2rlenv.tasksmith.models import Options, Panel


@pytest.fixture
def prepared(tmp_path):
    source = {
        "url": "https://github.com/example/project/pull/7",
        "repo": "https://github.com/example/project",
        "head": "b" * 40,
        "base": "a" * 40,
        "source_diff": "diff --git a/src/core.py b/src/core.py\n--- a/src/core.py\n+++ b/src/core.py\n@@ -1 +1 @@\n-value=1\n+value=2\n",
        "source_files": ["src/core.py"],
        "source_operations": [{"path": "src/core.py", "operation": "modified"}],
        "workspace_strategy": "head_minus_source_patch",
    }
    identity = {key: source[key] for key in ("url", "head", "source_diff")}
    source["id"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    task = write_bundle(
        TaskBundle(
            name="prepared",
            org="tests",
            instruction="Restore the promised public value.",
            metadata={
                "recipe": "tasksmith",
                "recipe_version": "1",
                "source_url": source["url"],
                "source_head": source["head"],
                "source_base": source["base"],
                "workspace_strategy": source["workspace_strategy"],
                "source_diff_sha256": hashlib.sha256(source["source_diff"].encode()).hexdigest(),
                "reward_kinds": ["test_execution"],
            },
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 1\n", executable=True),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
            },
        ),
        tmp_path / "tasks",
    )
    probes = ProbeManifest.model_validate(
        {
            "bundle_hash": task_identity(task),
            "probes": [
                {
                    "name": name,
                    "kind": kind,
                    "focus": "general",
                    "rationale": "Check the promised public value using this preserved implementation.",
                    "evidence": [{"path": "instruction.md", "quote": "promised public value"}],
                    "script": f"printf 'value = {value}\\n' > /workspace/src/core.py",
                }
                for name, kind, value in (
                    ("wrong-zero", "wrong_solution", "0"),
                    ("valid-addition", "valid_alternative", "1 + 1"),
                )
            ],
        }
    )
    return SimpleNamespace(task=task, source=source, probes=probes)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    campaign = tmp_path / "campaign"
    BudgetLedger(campaign / "budget.sqlite3", limit_usd="100")
    options = Options(worker_reservation_usd="3")
    options.quality.max_probes = 2
    value = runner_module.Tasksmith(tmp_path / "run", campaign, options, tmp_path / "runtime.whl")
    monkeypatch.setattr(runner_module, "resolve_llm_api_key", lambda *_: "test-only")
    monkeypatch.setattr(runner_module, "check_runtime_wheel", lambda *_: "frozen-runtime")
    monkeypatch.setattr(
        "repo2rlenv.tasksmith.author.external_agent.runtime_path", lambda *_: tmp_path
    )
    return value


def arguments(prepared):
    return {
        "source_records": [prepared.source],
        "prepared_task": prepared.task,
        "prepared_probes": prepared.probes,
    }


@pytest.mark.parametrize("invalid", ["missing_task", "hash", "count", "duplicate", "new_focus"])
def test_invalid_bindings_stop_at_admission_and_before_runner_work(
    invalid,
    prepared,
    runner,
    monkeypatch,
):
    args = arguments(prepared)
    options = runner.options
    if invalid == "missing_task":
        args["prepared_task"] = None
    elif invalid == "hash":
        args["prepared_probes"] = prepared.probes.model_copy(
            update={"bundle_hash": "sha256:" + "0" * 64}
        )
    elif invalid == "count":
        options.quality.max_probes = 1
    elif invalid == "duplicate":
        args["prepared_probes"] = prepared.probes.model_copy(deep=True)
        args["prepared_probes"].probes[1].name = args["prepared_probes"].probes[0].name
    else:
        options.required_probe_focus = ["model_behavior"]
    message = {
        "missing_task": "require a prepared task",
        "hash": "different task identity",
        "count": "exceed quality.max_probes",
        "duplicate": "names must be unique",
        "new_focus": "already include required_probe_focus",
    }[invalid]
    monkeypatch.setattr(runner, "_run", lambda *_: pytest.fail("Provider-capable run reached"))
    with pytest.raises(ValueError, match=message):
        batch.Candidate(
            url=prepared.source["url"],
            options=options,
            source_record=prepared.source,
            prepared_task=args["prepared_task"],
            prepared_probes=args["prepared_probes"],
        )
    with pytest.raises(ValueError, match=message):
        runner.run(Panel(name="prepared", prs=[prepared.source["url"]]), **args)
    assert not runner.directory.exists()
    assert runner.ledger.status()["operations"] == []


def test_trial_rewards_are_not_a_prepared_probe_input(prepared, runner):
    value = prepared.probes.model_dump(mode="json")
    value["trials"] = [{"reward": 1.0, "role": "probe"}]
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        batch.Candidate(
            url=prepared.source["url"],
            options=runner.options,
            source_record=prepared.source,
            prepared_task=prepared.task,
            prepared_probes=value,
        )
    assert runner.ledger.status()["operations"] == []


@pytest.mark.parametrize("required_focus", [[], ["model_behavior"]])
def test_definitions_reach_quality_and_changed_definitions_cannot_resume(
    prepared,
    runner,
    monkeypatch,
    required_focus,
):
    if required_focus:
        from repo2rlenv.quality.loop.requirements import with_probe_requirements

        prepared.task = with_probe_requirements(
            prepared.task,
            runner.directory.parent / "annotated" / prepared.task.name,
            required_focus,
        )
        prepared.probes.bundle_hash = task_identity(prepared.task)
        prepared.probes.probes[0].focus = "model_behavior"
        runner.options.required_probe_focus = required_focus
    seen, worker_calls = [], []

    class Loop:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, task, **kwargs):
            seen.append((task, kwargs, json.loads(kwargs["probes"].read_text())))
            return LoopResult(
                status="reviewed",
                source_hash=task_identity(task),
                bundle_hash=task_identity(task),
                task_path=str(task),
                repairs=0,
                review=None,
                trials=[],
                reasons=["Fixture only"],
                accounted_usd="0",
                reserved_usd="0",
            )

    monkeypatch.setattr(runner_module, "QualityLoop", Loop)
    monkeypatch.setattr(runner_module, "RemoteTrials", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(runner, "ready_worker", lambda: worker_calls.append("ready"))
    panel = Panel(name="prepared", prs=[prepared.source["url"]])
    result = runner.run(panel, **arguments(prepared))
    assert result["candidates"][0]["status"] == "reviewed"
    assert len(seen) == 1 and worker_calls == ["ready"]
    assert seen[0][2] == prepared.probes.model_dump(mode="json")
    assert set(seen[0][1]) == {"probes", "resume"}
    frozen = json.loads((runner.directory / "panel.json").read_text())["configuration"]
    imported = frozen["generation_inputs"][prepared.source["id"]]
    assert imported["probes"] == prepared.probes.model_dump(mode="json")["probes"]
    assert "trials" not in imported
    changed = prepared.probes.model_copy(deep=True)
    changed.probes[0].script += "\n# changed definition"
    with pytest.raises(ValueError, match="Pilot configuration changed"):
        runner.run(panel, **{**arguments(prepared), "prepared_probes": changed})
    assert len(seen) == 1 and worker_calls == ["ready"]
    assert runner.ledger.status()["operations"] == []


def test_candidate_dispatch_forwards_typed_definitions(prepared, runner, tmp_path, monkeypatch):
    seen = {}

    class FakeTasksmith:
        def __init__(self, directory, campaign, options, wheel):
            self.directory = directory
            self.ledger = runner.ledger

        def run(self, panel, **kwargs):
            seen.update(kwargs)
            save_record(self.directory / "report.json", {"candidates": [{"status": "reviewed"}]})

    selected = batch.Candidate(
        url=prepared.source["url"],
        options=runner.options,
        source_record=prepared.source,
        prepared_task=prepared.task,
        prepared_probes=prepared.probes,
    )
    monkeypatch.setattr(runner_module, "Tasksmith", FakeTasksmith)
    monkeypatch.setattr(batch, "_controller_identity", lambda: "controller")
    monkeypatch.setattr(batch, "check_runtime_wheel", lambda _: "runtime")
    config = {
        "directory": str(tmp_path / "batch"),
        "campaign": str(runner.ledger.path.parent),
        "wheel": str(tmp_path / "runtime.whl"),
        "runtime": "runtime",
        "controller_sha256": "controller",
        "plan": {"max_spend_usd": "20"},
    }
    batch._run_candidate(config, selected.model_dump(mode="json"))
    assert seen["prepared_probes"] == prepared.probes
    assert isinstance(seen["prepared_probes"], ProbeManifest)
    assert seen["prepared_task"] == prepared.task.resolve()
    assert seen["source_records"] == [prepared.source]
    assert seen["reuse_evidence"] is False and seen["generation_run"] is None
    assert runner.ledger.status()["operations"] == []


def test_batch_configuration_freezes_inline_definitions(prepared, runner, tmp_path, monkeypatch):
    wheel = tmp_path / "owned.whl"
    wheel.write_text("owned runtime fixture")
    monkeypatch.setattr(batch, "check_runtime_wheel", digest)
    monkeypatch.setattr(batch, "_controller_identity", lambda: "controller")
    monkeypatch.setattr(batch, "_freeze_controller", lambda *_: {})
    monkeypatch.setattr(
        batch, "_supervise_candidate", lambda *_: pytest.fail("Unexpected dispatch")
    )
    candidate = batch.Candidate(
        url=prepared.source["url"],
        options=runner.options,
        source_record=prepared.source,
        prepared_task=prepared.task,
        prepared_probes=prepared.probes,
    )
    plan = batch.BatchPlan(name="prepared", candidates=[candidate], max_spend_usd="1")
    output = tmp_path / "batch"
    result = batch.run_batch(
        plan, output, runner.ledger.path.parent, wheel, on_event=lambda _: None
    )
    assert result["stop_reason"] == "budget_headroom"
    frozen = json.loads((output / "configuration.json").read_text())
    assert frozen["plan"]["candidates"][0]["prepared_probes"] == prepared.probes.model_dump(
        mode="json"
    )
    changed = plan.model_copy(deep=True)
    changed.candidates[0].prepared_probes.probes[0].rationale += " Changed rationale."
    with pytest.raises(ValueError, match="Frozen batch inputs changed"):
        batch.run_batch(changed, output, runner.ledger.path.parent, wheel, on_event=lambda _: None)
    assert runner.ledger.status()["operations"] == []
