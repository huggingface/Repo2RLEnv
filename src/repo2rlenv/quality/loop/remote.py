"""Lazy, reusable Modal/Daytona execution for the quality loop."""

from __future__ import annotations

import json
from pathlib import Path

from repo2rlenv.execution.artifacts import check_runtime_wheel, install_runtime, runtime_python
from repo2rlenv.execution.base import WorkerSpec, connect_worker
from repo2rlenv.execution.harbor import run_trial
from repo2rlenv.execution.lifecycle import prepare_docker, provision_worker, stop_worker
from repo2rlenv.quality.loop.artifacts import import_trial
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.quality.loop.models import LoopOptions, TrialRecord


class RemoteTrials:
    def __init__(
        self,
        directory: Path,
        budget: RunBudget,
        options: LoopOptions,
        *,
        wheel: Path,
        provider: str = "modal",
        worker_receipt: Path | None = None,
        worker_reservation_usd: str = "3.00",
    ):
        self.directory, self.budget, self.options = directory, budget, options
        self.wheel, self.provider, self.receipt = wheel, provider, worker_receipt
        self.worker_reservation = worker_reservation_usd
        self.owns_worker = worker_receipt is None
        self.worker = None
        self.python = None

    def identity(self) -> dict:
        return {
            "provider": self.provider,
            "runtime_sha256": check_runtime_wheel(self.wheel),
            "worker_receipt": str(self.receipt.resolve()) if self.receipt else None,
            "worker_reservation_usd": self.worker_reservation,
        }

    def _ready(self):
        if self.worker is not None:
            return
        check_runtime_wheel(self.wheel)
        if self.receipt is None:
            workers = self.directory / "workers"
            # Old terminated workers are not revived. Uncertain creates are never repeated.
            index = 0
            while True:
                name = f"{self.budget.prefix}-w{index}"
                path = workers / f"{name}.json"
                if not path.exists():
                    self.worker, self.receipt = provision_worker(
                        WorkerSpec(
                            provider=self.provider,
                            name=name,
                            cpus=2,
                            memory_mb=4096,
                            timeout_sec=7200,
                        ),
                        workers,
                        self.budget,
                        reserve_usd=self.worker_reservation,
                    )
                    break
                record = json.loads(path.read_text())
                if record["state"] == "terminated":
                    index += 1
                    continue
                self.receipt = path
                break
        if self.worker is None:
            record = json.loads(self.receipt.read_text())
            if (
                record["state"] != "running"
                or Path(record["ledger"]).resolve() != self.budget.path.resolve()
            ):
                raise ValueError("Remote validation needs a running worker in this campaign")
            self.worker = connect_worker(record["spec"]["provider"], record["worker_id"])
        prepare_docker(self.worker)
        self.python = runtime_python(
            install_runtime(self.worker, self.wheel, self.directory / "runtime")
        )

    def run(self, task: Path, role: str, key: str) -> TrialRecord:
        output = self.directory / "trials" / key
        if (output / "trial.json").exists():
            return self._read(output, task, role)
        self._ready()
        run_trial(
            self.worker,
            task,
            output,
            trial_id=f"{self.budget.prefix}-{key}",
            agent={
                "baseline": "nop",
                "oracle": "oracle",
                "probe": "oracle",
                "rollout": "terminus-2",
            }[role],
            model=self.options.solver_model if role == "rollout" else None,
            ledger=self.budget,
            reservation_usd=self.options.solver_reservation_usd,
            max_turns=self.options.max_turns,
            max_tokens=self.options.solver_tokens,
            timeout_sec=self.options.trial_timeout_sec,
            python=self.python,
        )
        return self._read(output, task, role)

    @staticmethod
    def _read(output: Path, task: Path, role: str) -> TrialRecord:
        record = import_trial(output, task, "oracle" if role == "probe" else role)
        updates = {"role": role}
        if role == "probe":
            log = Path(record.result).parent / "agent/oracle.txt"
            updates["probe_installed"] = (
                log.is_file() and "__QUALITY_PROBE_COMPLETED__" in log.read_text()
            )
        return record.model_copy(update=updates)

    def close(self):
        if self.owns_worker and self.worker is not None and self.receipt is not None:
            stop_worker(self.receipt, self.budget)
            self.worker = None
