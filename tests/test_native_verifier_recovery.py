"""Exercise recovery against Harbor's real controller; never start target sandboxes."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor")
pytest.importorskip("modal")

from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.loop.artifacts import digest, import_trial, task_identity
from repo2rlenv.quality.loop.cli import execution_backend
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.quality.loop.models import LoopOptions
from repo2rlenv.quality.loop.native import NativeModalTrials
from repo2rlenv.quality.loop.native_recovery import (
    resume_verifier,
    seal_verifier_inputs,
    verifier_inputs,
)


@pytest.fixture
def gpu_task(tmp_path):
    resources = {"gpus": 1, "gpu_types": ["L4"], "network_mode": "no-network"}
    return write_bundle(
        TaskBundle(
            name="gpu-example",
            org="tests",
            instruction="Compute the sum on CUDA.\n",
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "environment/verifier/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
            },
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
            environment=resources,
            verifier={"environment_mode": "separate", "environment": resources},
            artifacts=[{"source": "/workspace/src"}],
        ),
        tmp_path / "tasks",
    )


@pytest.fixture
def interrupted(tmp_path, gpu_task):
    from harbor.models.trial.config import TrialConfig
    from harbor.models.trial.result import TrialResult

    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10")
    budget = RunBudget(ledger, "recovery-test", "10")
    output = tmp_path / "quality/trials/r0-rollout"
    root = output / "owned-original/original"
    for name in ("artifacts/workspace/src", "agent"):
        (root / name).mkdir(parents=True)
    (root / "artifacts/workspace/src/example.py").write_text("answer = 4\n")
    save_record(
        root / "artifacts/manifest.json",
        [
            {
                "source": "/workspace/src",
                "destination": "artifacts/workspace/src",
                "type": "directory",
                "status": "ok",
                "service": None,
            }
        ],
    )
    save_record(root / "agent/trajectory.json", {"steps": [{"content": "Completed edit"}]})
    now = datetime.now(UTC).isoformat()
    config = TrialConfig.model_validate(
        {
            "task": {"path": str(gpu_task)},
            "trial_name": "original",
            "trials_dir": str(root.parent),
            "agent": {"name": "terminus-2", "model_name": "anthropic/claude-sonnet-4-6"},
            "environment": {
                "import_path": "repo2rlenv.execution.harbor_modal:MeteredModalEnvironment"
            },
        }
    )
    result = TrialResult.model_validate(
        {
            "task_name": "gpu-example",
            "trial_name": "original",
            "trial_uri": root.as_uri(),
            "task_id": {"path": str(gpu_task)},
            "task_checksum": "fixture",
            "config": config.model_dump(mode="json"),
            "agent_info": {"name": "terminus-2", "version": "fixture"},
            "agent_result": {"cost_usd": 0.25, "n_input_tokens": 100, "n_output_tokens": 20},
            "agent_execution": {"started_at": now, "finished_at": now},
            "exception_info": {
                "exception_type": "BudgetExceeded",
                "exception_message": "Denied",
                "exception_traceback": "fixture verifier allocation",
                "occurred_at": now,
            },
        }
    )
    (root / "result.json").write_text(result.model_dump_json(indent=2))
    save_record(
        output / "trial.json",
        {
            "trial_id": "owned-original",
            "state": "completed",
            "bundle_hash": task_identity(gpu_task),
            "result": str(root / "result.json"),
            "result_sha256": digest(root / "result.json"),
            "agent": "terminus-2",
            "model": "anthropic/claude-sonnet-4-6",
            "execution": {"provider": "native-modal"},
        },
    )
    for name, state in (("learner", "terminated"), ("__verifier__trial", "reservation_failed")):
        save_record(
            output / "allocations" / (name + ".json"),
            {
                "state": state,
                "provider_name": name,
                "ledger": str(budget.path),
            },
        )
    seal_verifier_inputs(gpu_task, output, budget)
    return SimpleNamespace(task=gpu_task, output=output, root=root, budget=budget, ledger=ledger)


def test_cli_routes_gpu_tasks_without_cpu_wheel(gpu_task):
    assert (
        execution_backend(gpu_task, provider="modal", wheel=None, worker_receipt=None)
        == "native-modal"
    )
    with pytest.raises(ValueError, match="require --provider modal"):
        execution_backend(gpu_task, provider="daytona", wheel=None, worker_receipt=None)
    with pytest.raises(ValueError, match="worker-receipt"):
        execution_backend(
            gpu_task, provider="modal", wheel=None, worker_receipt=Path("worker.json")
        )


@pytest.mark.parametrize("provider", ["modal", "daytona"])
def test_cpu_route_requires_worker_runtime(gpu_task, provider):
    import tomllib

    import tomli_w

    path = gpu_task / "task.toml"
    config = tomllib.loads(path.read_text())
    for environment in (config["environment"], config["verifier"]["environment"]):
        environment.pop("gpus")
        environment.pop("gpu_types")
    path.write_text(tomli_w.dumps(config))
    with pytest.raises(ValueError, match=r"CPU .* require --runtime-wheel"):
        execution_backend(gpu_task, provider=provider, wheel=None, worker_receipt=None)
    assert (
        execution_backend(
            gpu_task, provider=provider, wheel=Path("runtime.whl"), worker_receipt=None
        )
        == provider
    )


@pytest.mark.parametrize(
    "mutation",
    ["submission", "trace", "mode", "symlink", "allocation", "result", "task", "manifest"],
)
def test_changed_evidence_blocks_resume_before_dispatch(interrupted, monkeypatch, mutation):
    from harbor.trial.trial import Trial

    def no_dispatch(*args):
        pytest.fail("Invalid evidence must be rejected before Harbor constructs a trial")

    monkeypatch.setattr(Trial, "create", no_dispatch)
    run = interrupted
    submission = run.root / "artifacts/workspace/src/example.py"
    if mutation == "submission":
        submission.write_text("answer = 99\n")
    elif mutation == "trace":
        (run.root / "agent/trajectory.json").write_text("{}")
    elif mutation == "mode":
        submission.chmod(0o755)
    elif mutation == "symlink":
        submission.unlink()
        submission.symlink_to(run.root / "result.json")
    elif mutation == "allocation":
        save_record(run.output / "allocations/unknown.json", {"state": "creation_uncertain"})
    elif mutation == "result":
        (run.root / "result.json").write_text("{}")
    elif mutation == "task":
        (run.task / "instruction.md").write_text("Different task")
    else:
        save_record(run.root / "artifacts/manifest.json", [{"status": "error"}])
    with pytest.raises(ValueError):
        asyncio.run(
            resume_verifier(
                run.task, run.output, run.output / "resume", run.budget, LoopOptions(), {}
            )
        )
    assert not (run.output / "resume").exists()
    assert not run.ledger.status()["operations"]


def test_verifier_continuation_preserves_solver_and_runs_once(interrupted, monkeypatch):
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.models.verifier.result import VerifierResult
    from harbor.trial.single_step import SingleStepTrial

    from repo2rlenv.execution.harbor_modal import MeteredModalEnvironment

    calls = []

    async def forbid(*args, **kwargs):
        pytest.fail("Recovery must not start a learner, collect new source or call its model")

    async def verify(trial):
        calls.append("verifier")
        assert (
            trial.paths.artifacts_dir / "workspace/src/example.py"
        ).read_text() == "answer = 4\n"
        trial.result.verifier_result = VerifierResult(rewards={"reward": 0.0})

    monkeypatch.setattr(Terminus2, "_init_llm", lambda *args, **kwargs: object())
    monkeypatch.setattr(SingleStepTrial, "_run_agent", forbid)
    monkeypatch.setattr(SingleStepTrial, "run", forbid)
    monkeypatch.setattr(MeteredModalEnvironment, "start", forbid)
    monkeypatch.setattr(SingleStepTrial, "_run_verifier", verify)
    run = interrupted
    originals = verifier_inputs(run.task, run.output, run.budget)
    runner = NativeModalTrials(run.output.parents[1], run.budget, LoopOptions())
    result = runner.run(run.task, "rollout", "r0-rollout")
    again = runner.run(run.task, "rollout", "r0-rollout")
    assert calls == ["verifier"] and result == again
    assert result.reward == 0.0 and result.exception_type is None
    recovered = json.loads(Path(result.result).read_text())
    original = json.loads((run.root / "result.json").read_text())
    for field in ("id", "agent_execution", "agent_result", "agent_info"):
        assert recovered[field] == original[field]
    assert (Path(result.result).parent / "original-interrupted-result.json").read_bytes() == (
        run.root / "result.json"
    ).read_bytes()
    assert verifier_inputs(run.task, run.output, run.budget) == originals
    root = Path(result.result).parent
    provenance = json.loads((root / "resumption.json").read_text())
    assert digest(root / "original-trial-receipt.json") == provenance["receipt_sha256"]
    assert digest(root / "original-verifier-inputs.json") == provenance["input_seal_sha256"]
    assert provenance["artifact_hashes"]["workspace/src/example.py"] == digest(
        root / "artifacts/workspace/src/example.py"
    )
    assert not run.ledger.status()["operations"]


def test_unsealed_old_trial_is_retained_without_reexecution(interrupted):
    run = interrupted
    (run.output / "verifier-inputs.json").unlink()
    runner = NativeModalTrials(run.output.parents[1], run.budget, LoopOptions())
    result = runner.run(run.task, "rollout", "r0-rollout")
    assert result.exception_type == "BudgetExceeded"
    assert not (run.output / "verifier-resume").exists()


def test_cancelled_continuation_keeps_cleanup_evidence_and_does_not_retry(interrupted, monkeypatch):
    from harbor.trial.trial import Trial

    from repo2rlenv.execution.harbor_modal import ACCOUNTING

    calls = []

    async def stop():
        pass

    async def cancel(_):
        calls.append("dispatch")
        ACCOUNTING.get().environments.append(
            SimpleNamespace(record={"state": "creation_uncertain"}, stop=stop)
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(Trial, "create", cancel)
    run = interrupted
    runner = NativeModalTrials(run.output.parents[1], run.budget, LoopOptions())
    with pytest.raises(asyncio.CancelledError):
        runner.run(run.task, "rollout", "r0-rollout")
    receipt = run.output / "verifier-resume/trial.json"
    assert json.loads(receipt.read_text())["state"] == "cleanup_uncertain"
    with pytest.raises(ValueError, match="incomplete"):
        runner.run(run.task, "rollout", "r0-rollout")
    assert calls == ["dispatch"] and ACCOUNTING.get() is None


@pytest.mark.parametrize(
    "error", [BudgetExceeded("Denied again"), ConnectionError("Lost response")]
)
def test_continuation_failure_is_retained_not_repeated(interrupted, monkeypatch, error):
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.trial.single_step import SingleStepTrial

    calls = []

    async def fail(trial):
        calls.append("attempt")
        raise error

    monkeypatch.setattr(Terminus2, "_init_llm", lambda *args, **kwargs: object())
    monkeypatch.setattr(SingleStepTrial, "_run_verifier", fail)
    run = interrupted
    runner = NativeModalTrials(run.output.parents[1], run.budget, LoopOptions())
    result = runner.run(run.task, "rollout", "r0-rollout")
    assert result.exception_type == type(error).__name__
    assert runner.run(run.task, "rollout", "r0-rollout") == result
    assert calls == ["attempt"]
    assert import_trial(run.output, run.task, "rollout").exception_type == "BudgetExceeded"
