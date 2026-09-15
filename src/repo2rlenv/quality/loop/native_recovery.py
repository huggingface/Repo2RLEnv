"""Resume a denied private verifier without repeating a completed learner.

Harbor 0.22's single-step verifier API is isolated here. A seal written at
collection time binds the submission and trace; older unsealed trials require
explicit reconciliation. Unknown provider outcomes are never automatically retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import stat
from pathlib import Path

from repo2rlenv.execution.harbor import read_trial
from repo2rlenv.execution.lifecycle import now, save_record
from repo2rlenv.quality.loop.artifacts import digest, import_trial, task_identity


def evidence_tree(root: Path) -> dict:
    """Hash ordinary files and modes, rejecting links and incomplete submissions."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Recovery evidence must be an ordinary directory")
    files, size = {}, 0
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Recovery evidence contains a link or special file")
        size += info.st_size
        if size > 1_000_000_000 or len(files) >= 100_000:
            raise ValueError("Recovery evidence exceeds the collection limit")
        files[path.relative_to(root).as_posix()] = {
            "sha256": digest(path),
            "bytes": info.st_size,
            "mode": stat.S_IMODE(info.st_mode),
        }
    if not files:
        raise ValueError("Recovery evidence is empty")
    return files


def verifier_inputs(task: Path, output: Path, budget) -> dict:
    """Prove this is a completed native solver followed by a denied allocation."""
    from repo2rlenv.quality.loop.native import (
        model_cost_after_verifier_denial,
        validate_native_task,
    )

    if validate_native_task(task).has_steps:
        raise ValueError("Verifier continuation requires a single-step Harbor task")
    receipt = output / "trial.json"
    record = json.loads(receipt.read_text())
    trial = import_trial(receipt, task, "rollout")
    result = Path(trial.result)
    raw = json.loads(result.read_text())
    artifacts = evidence_tree(result.parent / "artifacts")
    agent = evidence_tree(result.parent / "agent")
    claims = [
        path
        for path in (output / "allocations").glob("*.json")
        if not path.name.endswith(".cost.json")
    ]
    if (
        record.get("execution", {}).get("provider") != "native-modal"
        or trial.agent != "terminus-2"
        or raw.get("verifier_result") is not None
        or raw.get("step_results")
        or model_cost_after_verifier_denial(result, output / "allocations") is None
        or any(
            Path(json.loads(path.read_text()).get("ledger", "")).resolve() != budget.path.resolve()
            for path in claims
        )
    ):
        raise ValueError("Trial is not a proven native verifier allocation denial in this campaign")
    manifest = result.parent / "artifacts/manifest.json"
    items = json.loads(manifest.read_text())
    if (
        not isinstance(items, list)
        or not items
        or any(not isinstance(item, dict) or item.get("status") != "ok" for item in items)
    ):
        raise ValueError("Submission artifact collection is incomplete")
    for item in items:
        destination = Path(item.get("destination", ""))
        if (
            not destination.parts
            or destination.is_absolute()
            or ".." in destination.parts
            or destination.parts[0] != "artifacts"
        ):
            raise ValueError("Artifact manifest contains an invalid destination")
        if not (result.parent / destination).exists():
            raise ValueError("Artifact manifest destination is missing")
    if not (result.parent / "agent/trajectory.json").is_file():
        raise ValueError("Completed learner trace is missing")
    return {
        "schema_version": 1,
        "bundle_hash": task_identity(task),
        "receipt_sha256": digest(receipt),
        "result_sha256": trial.result_sha256,
        "allocations": {path.name: digest(path) for path in claims},
        "artifacts": artifacts,
        "agent": agent,
    }


def seal_verifier_inputs(task: Path, output: Path, budget):
    """Persist once, as the controller collects a failed verifier result."""
    path = output / "verifier-inputs.json"
    if path.exists():
        raise ValueError("Verifier recovery inputs were already sealed")
    save_record(path, verifier_inputs(task, output, budget))


async def resume_verifier(task: Path, source: Path, destination: Path, budget, options, execution):
    """One continuation; never dispatch an agent or reserve model tokens."""
    from harbor.models.trial.config import TrialConfig
    from harbor.models.trial.result import TrialResult
    from harbor.trial.single_step import SingleStepTrial
    from harbor.trial.trial import Trial

    from repo2rlenv.execution.harbor_modal import ACCOUNTING, NativeAccounting

    sealed = json.loads((source / "verifier-inputs.json").read_text())
    if sealed != verifier_inputs(task, source, budget):
        raise ValueError("Saved verifier recovery evidence changed after collection")
    imported = import_trial(source, task, "rollout")
    original = Path(imported.result)
    raw = TrialResult.model_validate_json(original.read_text())
    # Construct only owned adapters from known fields; never load import paths,
    # skills, hooks or environment overrides from a saved result.
    trial_id = "resume-" + hashlib.sha256(str(destination.resolve()).encode()).hexdigest()[:24]
    config = TrialConfig.model_validate(
        {
            "task": {"path": str(task.resolve())},
            "trial_name": "r2e-" + trial_id,
            "trials_dir": str(destination / trial_id),
            "agent": {
                "name": "terminus-2",
                "model_name": imported.model,
                "kwargs": {"record_terminal_session": False},
            },
            "environment": {
                "import_path": "repo2rlenv.execution.harbor_modal:MeteredModalEnvironment",
                "kwargs": {
                    "app_name": "repo2rlenv-owned-generation",
                    "sandbox_timeout_secs": min(1800, options.trial_timeout_sec),
                },
            },
        }
    )
    destination.mkdir(parents=True, exist_ok=False)
    receipt = destination / "trial.json"
    provenance = {
        "receipt": str((source / "trial.json").resolve()),
        "receipt_sha256": sealed["receipt_sha256"],
        "result": str(original),
        "result_sha256": sealed["result_sha256"],
        "input_seal_sha256": digest(source / "verifier-inputs.json"),
        "artifact_hashes": {path: item["sha256"] for path, item in sealed["artifacts"].items()},
        "model_execution_reused": True,
    }
    record = {
        "trial_id": trial_id,
        "bundle_hash": imported.bundle_hash,
        "agent": imported.agent,
        "model": imported.model,
        "state": "claimed",
        "started_at": now(),
        "execution": {**execution, "mode": "verifier-only", "model_dispatched": False},
        "resumed_from": provenance,
    }
    save_record(receipt, record)
    save_record(destination / "resumption.json", provenance)
    accounting = NativeAccounting(budget, destination / "allocations")
    token = ACCOUNTING.set(accounting)
    trial = None
    cancelled = False
    try:
        trial = await Trial.create(config)
        if not isinstance(trial, SingleStepTrial):
            raise ValueError("Verifier continuation requires a single-step Harbor task")
        trial._init_result()
        for name in ("artifacts", "agent"):
            target = trial.paths.trial_dir / name
            shutil.copytree(original.parent / name, target, dirs_exist_ok=True)
            if evidence_tree(target) != sealed[name]:
                raise ValueError("Verifier recovery copy did not preserve evidence")
        result = raw.model_copy(deep=True)
        result.config, result.trial_name = config, config.trial_name
        result.trial_uri = trial.paths.trial_dir.resolve().as_uri()
        result.exception_info, result.finished_at, result.verifier = None, None, None
        trial._result = result
        shutil.copyfile(original, trial.paths.trial_dir / "original-interrupted-result.json")
        shutil.copyfile(
            source / "trial.json", trial.paths.trial_dir / "original-trial-receipt.json"
        )
        shutil.copyfile(
            source / "verifier-inputs.json", trial.paths.trial_dir / "original-verifier-inputs.json"
        )
        save_record(trial.paths.trial_dir / "resumption.json", provenance)
        record["state"] = "dispatched"
        save_record(receipt, record)
        # Deliberately skip trial.run(), agent setup, agent.run() and artifact collection.
        try:
            await asyncio.wait_for(trial._run_verifier(), timeout=options.trial_timeout_sec)
        except Exception as exc:
            trial._record_exception(exc)
        result.finished_at = trial._now()
        trial.paths.result_path.write_text(result.model_dump_json(indent=2))
        evidence = read_trial(destination / trial_id)
        if verifier_inputs(task, source, budget) != sealed:
            raise ValueError("Original recovery evidence changed during verification")
        record.update(
            state="completed",
            finished_at=now(),
            result=str(evidence.result.resolve()),
            result_sha256=digest(evidence.result),
            reward=evidence.reward,
            exception_type=evidence.exception_type,
        )
        save_record(receipt, record)
    except BaseException as exc:
        cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
        record.update(state="interrupted", interrupted_at=now())
        save_record(receipt, record)
        raise
    finally:
        try:
            results = await asyncio.gather(
                *(environment.stop() for environment in accounting.environments),
                return_exceptions=True,
            )
            if any(isinstance(value, BaseException) for value in results) or any(
                environment.record
                and environment.record["state"] not in {"terminated", "build_failed"}
                for environment in accounting.environments
            ):
                record["state"] = "cleanup_uncertain"
                save_record(receipt, record)
                if not cancelled:
                    raise RuntimeError("Verifier continuation cleanup needs reconciliation")
        finally:
            if trial is not None:
                trial._close_logger_handler()
            ACCOUNTING.reset(token)
