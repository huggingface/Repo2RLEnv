"""One remote Harbor trial with task-bound evidence and explicit model accounting."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

from repo2rlenv.auth import resolve_llm_api_key
from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.execution.artifacts import unpack_evidence
from repo2rlenv.execution.base import RemoteWorker
from repo2rlenv.execution.jobs import launch_job, observe_job
from repo2rlenv.execution.lifecycle import now, save_record
from repo2rlenv.spec.input import LLMSpec


@dataclass(frozen=True)
class TrialEvidence:
    reward: float | None
    exception_type: str | None
    result: Path
    cost_usd: float | None

    @property
    def completed(self) -> bool:
        return self.exception_type is None and self.reward is not None


def read_trial(directory: Path) -> TrialEvidence:
    results = list(directory.glob("*/result.json"))
    if len(results) != 1:
        raise ValueError(f"Expected exactly one Harbor trial, found {len(results)}")
    path = results[0]
    data = json.loads(path.read_text())
    exception = (data.get("exception_info") or {}).get("exception_type")
    reward = (data.get("verifier_result") or {}).get("rewards", {}).get("reward")
    cost = (data.get("agent_result") or {}).get("cost_usd")
    if reward is not None and (
        isinstance(reward, bool)
        or not isinstance(reward, (int, float))
        or not math.isfinite(reward)
    ):
        raise ValueError("Harbor reward must be numeric")
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ValueError("Harbor cost must be a finite nonnegative number")
    return TrialEvidence(reward, exception, path, cost)


def run_trial(
    worker: RemoteWorker,
    task: Path,
    output: Path,
    *,
    trial_id: str,
    agent: str = "nop",
    model: LLMSpec | None = None,
    ledger: BudgetLedger | None = None,
    reservation_usd: str = "4.00",
    max_turns: int = 12,
    max_tokens: int = 2048,
    timeout_sec: int = 900,
    probe: str | None = None,
    submitted_path: str | None = None,
    resume: bool = False,
    python: str = "python",
) -> TrialEvidence:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,60}", trial_id):
        raise ValueError("Trial ID must be a unique lowercase slug")
    if agent not in {"nop", "oracle", "terminus-2", "probe"}:
        raise ValueError("Use a supported deterministic or external Harbor agent")
    if (agent == "probe") != (probe is not None):
        raise ValueError("Probe trials require an explicit probe name")
    if probe is not None:
        from repo2rlenv.execution.probe_agent import probe_program

        probe_program(probe, submitted_path)
    if (agent == "terminus-2") != (model is not None):
        raise ValueError("Terminus-2 requires a model; nop and oracle do not")
    if not (1 <= max_turns <= 100 and 256 <= max_tokens <= 8192 and 30 <= timeout_sec <= 3600):
        raise ValueError("Trial limits are outside the supported bounded range")
    identity = inspect_bundle(task)
    if not identity["integrity_passed"]:
        raise ValueError("Task has changed since emission")
    from harbor.models.task.task import Task

    Task(task)  # Parse the exact pinned Harbor contract before dispatch.
    env = {"REPO2RLENV_REMOTE_WORKER": "1"}
    if model is not None:
        if model.provider not in {"openai", "anthropic"} or model.endpoint or model.fallback:
            raise ValueError("Blind trials currently support direct OpenAI/Anthropic routes")
        key = resolve_llm_api_key(model.provider, model.api_key_env)
        if not key or ledger is None:
            raise ValueError("A blind trial requires credentials and a campaign ledger")
        env[model.provider.upper() + "_API_KEY"] = key
    receipt = output / "trial.json"
    operation_id = "trial:" + trial_id
    record = {
        "trial_id": trial_id,
        "bundle_hash": identity["bundle_hash"],
        "worker_id": worker.id,
        "agent": agent,
        "probe": probe,
        "model": model.qualified_name if model else None,
        "submitted_path": submitted_path,
        "max_turns": max_turns,
        "max_tokens": max_tokens,
        "timeout_sec": timeout_sec,
        "runtime_python": python,
    }
    if output.exists():
        stored = json.loads(receipt.read_text())
        if not resume or stored.get("state") != "completed":
            raise FileExistsError("Reconcile the existing trial before another dispatch")
        if any(stored.get(key) != value for key, value in record.items()):
            raise ValueError("Stored trial belongs to different task, worker or execution settings")
        evidence = read_trial(output / trial_id)
        if hashlib.sha256(evidence.result.read_bytes()).hexdigest() != stored["result_sha256"]:
            raise ValueError("Stored trial evidence changed")
        return evidence
    output.mkdir(parents=True, exist_ok=False)
    record.update(started_at=now(), state="claimed")
    save_record(receipt, record)
    if model is not None:
        ledger.reserve(operation_id, reservation_usd, f"Blind Harbor trial {trial_id}")
    remote_dir = "/work/trials/" + trial_id
    remote_jobs = "/evidence/trials/" + trial_id
    try:
        archive = output / "task.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(task, arcname=task.name)
        worker.exec(["mkdir", "-p", "/work/trials", "/evidence/trials"], timeout=30).checked(
            "Trial directories"
        )
        worker.exec(["mkdir", remote_dir, remote_jobs], timeout=30).checked(
            "Claim unique remote trial"
        )
        worker.upload(archive, remote_dir + "/task.tar.gz")
        worker.exec(
            ["tar", "-xzf", remote_dir + "/task.tar.gz", "-C", remote_dir], timeout=30
        ).checked("Unpack owned task")
        command = [
            str(Path(python).with_name("harbor")) if python != "python" else "harbor",
            "run",
            "--path",
            remote_dir + "/" + task.name,
            "--agent",
            "repo2rlenv.execution.probe_agent:VerifierProbeAgent" if probe else agent,
            "--env",
            "repo2rlenv.execution.harbor_offline:OfflineDockerEnvironment",
            "--n-concurrent",
            "1",
            "--max-retries",
            "0",
            "--job-name",
            trial_id,
            "--jobs-dir",
            remote_jobs,
            "--quiet",
        ]
        if probe:
            command.extend(["--ak", "probe=" + probe])
            if submitted_path:
                command.extend(["--ak", "submitted_path=" + submitted_path])
        if model:
            command.extend(
                [
                    "--model",
                    model.qualified_name,
                    "--ak",
                    f"max_turns={max_turns}",
                    "--ak",
                    "record_terminal_session=false",
                    "--ak",
                    "llm_call_kwargs=" + json.dumps({"max_tokens": max_tokens}),
                ]
            )
        record.update(state="dispatched", command=command)
        save_record(receipt, record)
        launch_job(worker, remote_dir, command, timeout_sec=timeout_sec, env=env, python=python)
        status = observe_job(worker, remote_dir, timeout_sec=timeout_sec + 100)
        save_record(output / "remote-job.json", status)
        worker.download(remote_dir + "/stdout.txt", output / "stdout.txt")
        worker.download(remote_dir + "/stderr.txt", output / "stderr.txt")
        record["command_returncode"] = status.get("returncode")
        if status["state"] != "completed" or status.get("returncode") != 0:
            raise RuntimeError("Harbor job failed or needs cleanup; inspect remote-job.json")
        worker.exec(
            ["tar", "-czf", remote_dir + "/evidence.tar.gz", "-C", remote_jobs, trial_id],
            timeout=60,
        ).checked("Archive exact trial evidence")
        worker.download(remote_dir + "/evidence.tar.gz", output / "evidence.tar.gz")
        job = unpack_evidence(output / "evidence.tar.gz", output, root_name=trial_id)
        evidence = read_trial(job)
        record.update(
            state="completed",
            finished_at=now(),
            reward=evidence.reward,
            exception_type=evidence.exception_type,
            result=str(evidence.result),
            result_sha256=hashlib.sha256(evidence.result.read_bytes()).hexdigest(),
        )
        save_record(receipt, record)
        if model is not None:
            if evidence.cost_usd is not None and evidence.cost_usd > 0 and evidence.completed:
                ledger.settle(operation_id, evidence.cost_usd, evidence=str(receipt.resolve()))
            else:
                ledger.mark_uncertain(
                    operation_id, f"Incomplete trial or unknown usage: {receipt.resolve()}"
                )
        return evidence
    except BaseException:
        record.update(state="interrupted", interrupted_at=now())
        save_record(receipt, record)
        if model is not None:
            ledger.mark_uncertain(
                operation_id,
                f"Reconcile remote trial {remote_jobs}; {receipt.resolve()}",
            )
        raise
