"""Shared controller for remote repository generators and owned task authors."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.campaigns.events import ProgressEvent
from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.execution.artifacts import (
    check_runtime_wheel,
    install_runtime,
    runtime_python,
    unpack_evidence,
)
from repo2rlenv.execution.base import connect_worker
from repo2rlenv.execution.jobs import launch_job, observe_job
from repo2rlenv.execution.lifecycle import prepare_docker, save_record
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.spec.input import GenerationInput, RepositorySource


class RepositoryGenerationPipeline:
    recipe_id: str
    worker_module: str
    requires_bootstrap = False  # The existing bootstrap runs inside the worker.
    supported_languages = frozenset({LanguageHint.PYTHON})
    experimental = True
    native_supported = False

    def __init__(self, input: GenerationInput, options, bootstrap=None):
        if input.pipeline.recipe != self.recipe_id:
            raise ValueError(f"This pipeline requires --recipe {self.recipe_id}")
        if (
            not isinstance(input.source, RepositorySource)
            or not input.repo.url.startswith("https://github.com/")
            or input.repo.access == "private"
        ):
            raise ValueError(
                "This recipe currently supports public GitHub Python repository profiles"
            )
        if input.execution is None or input.llm is None:
            raise ValueError(
                "Owned generation requires execution settings and an issue-writing LLM"
            )
        self.input, self.options = input, options
        self.on_event: Callable[[ProgressEvent], None] = lambda event: None

    def set_event_callback(self, callback: Callable[[ProgressEvent], None]) -> None:
        self.on_event = callback

    def event(self, stage: str, state: str, message: str, **metrics) -> None:
        self.on_event(
            ProgressEvent(
                recipe=self.recipe_id, stage=stage, state=state, message=message, metrics=metrics
            )
        )

    def run(self, out_dir: Path) -> PipelineResult:
        execution = self.input.execution
        run = execution.campaign_dir / "runs" / execution.run_id
        receipt = run / "run.json"
        wheel_hash = check_runtime_wheel(execution.runtime_wheel)
        configuration = self.input.model_dump(mode="json")
        configuration["execution"].pop("resume", None)
        configuration["runtime_sha256"] = wheel_hash
        config_hash = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
        if receipt.exists():
            if not execution.resume:
                raise FileExistsError("Run already exists; resume it explicitly")
            record = json.loads(receipt.read_text())
            if record["config_sha256"] != config_hash:
                raise ValueError("Run configuration or runtime code changed; use a new run ID")
        else:
            run.mkdir(parents=True, exist_ok=True)
            with receipt.open("x"):
                pass
            record = {
                "state": "claimed",
                "config_sha256": config_hash,
                "configuration": configuration,
                "exported": {},
            }
            save_record(receipt, record)
        ledger = BudgetLedger(execution.campaign_dir / "budget.sqlite3")
        generation = run / execution.run_id
        if record["state"] not in {"downloaded", "exporting", "completed"}:
            worker_record = json.loads(execution.worker_receipt.read_text())
            if (
                worker_record["state"] != "running"
                or Path(worker_record["ledger"]).resolve() != ledger.path.resolve()
            ):
                raise ValueError("Run requires a running worker from the same campaign")
            if record.get("worker_id", worker_record["worker_id"]) != worker_record["worker_id"]:
                raise ValueError("A dispatched run must resume on its original worker")
            deadline = datetime.fromisoformat(worker_record["started_at"]) + timedelta(
                seconds=worker_record["spec"]["timeout_sec"]
            )
            remaining = int((deadline - datetime.now(UTC)).total_seconds())
            if remaining < 60:
                raise ValueError(
                    "Worker execution window expired; reconcile this run before dispatch"
                )
            timeout = min(execution.timeout_sec, remaining - 15)
            worker = connect_worker(worker_record["spec"]["provider"], worker_record["worker_id"])
            prepare_docker(worker)
            remote = "/work/generation/" + execution.run_id
            remote_output = "/evidence/generation/" + execution.run_id
            if record["state"] == "claimed":
                self.event("runtime", "started", "Install the verified owned runtime")
                install_runtime(worker, execution.runtime_wheel, run)
                worker.exec(
                    ["mkdir", "-p", "/work/generation", "/evidence/generation"], timeout=30
                ).checked("Generation parents")
                worker.exec(["mkdir", remote], timeout=30).checked("Claim remote generation")
                save_record(
                    run / "worker-config.json",
                    {
                        "repo": self.input.repo.model_dump(mode="json"),
                        "options": self.options.model_dump(mode="json"),
                    },
                )
                worker.upload(run / "worker-config.json", remote + "/config.json")
                record.update(
                    state="remote_dispatched",
                    worker_id=worker.id,
                    remote=remote,
                    remote_output=remote_output,
                )
                save_record(receipt, record)
                launch_job(
                    worker,
                    remote,
                    [
                        runtime_python(wheel_hash),
                        "-m",
                        self.worker_module,
                        remote + "/config.json",
                        remote_output,
                    ],
                    timeout_sec=timeout,
                    python=runtime_python(wheel_hash),
                )
            status = observe_job(
                worker,
                remote,
                event_file=remote_output + "/events.jsonl",
                timeout_sec=timeout + 10,
                on_event=self.on_event,
            )
            save_record(run / "remote-job.json", status)
            if status["state"] != "completed" or status.get("returncode") != 0:
                worker.download(remote + "/stderr.txt", run / "worker.stderr")
                raise RuntimeError("Remote generation failed; inspect the recorded worker stderr")
            worker.exec(
                [
                    "tar",
                    "-czf",
                    remote + "/generation.tar.gz",
                    "-C",
                    "/evidence/generation",
                    execution.run_id,
                ],
                timeout=120,
            ).checked("Archive generation evidence")
            worker.download(remote + "/generation.tar.gz", run / "generation.tar.gz")
            if not generation.exists():
                unpack_evidence(run / "generation.tar.gz", run, root_name=execution.run_id)
            record["state"] = "downloaded"
            save_record(receipt, record)
        result = json.loads((generation / "generation.json").read_text())
        skipped = Counter(item["reason"] for item in result["rejected"])
        record["state"] = "exporting"
        save_record(receipt, record)
        for candidate in result["candidates"]:
            if len(record["exported"]) >= self.options.target:
                break
            candidate_id = candidate["id"]
            if candidate_id in record["exported"]:
                task = Path(record["exported"][candidate_id]["path"])
                if (
                    inspect_bundle(task)["bundle_hash"]
                    != record["exported"][candidate_id]["bundle_hash"]
                ):
                    raise ValueError("An exported task changed after its receipt was written")
                continue
            self.event(
                "author",
                "started",
                "Author the task from recorded execution evidence",
                exported=len(record["exported"]),
            )
            try:
                task = self.author_export(generation, candidate, ledger, run, out_dir)
            except ValueError as exc:
                skipped["task_materialization"] += 1
                self.event("author", "failed", str(exc))
                continue
            record["exported"][candidate_id] = {
                "path": str(task.resolve()),
                "bundle_hash": inspect_bundle(task)["bundle_hash"],
            }
            save_record(receipt, record)
            self.event(
                "export", "completed", task.name, exported=len(record["exported"]), accepted=0
            )
        record.update(state="completed", skipped=dict(skipped), attempted=result["attempted"])
        save_record(receipt, record)
        return PipelineResult(
            candidates=result["attempted"],
            emitted=len(record["exported"]),
            skipped=sum(skipped.values()),
            out_dir=out_dir,
            skip_reasons=dict(skipped),
        )

    def author_export(self, generation, candidate, ledger, run, out_dir) -> Path:
        raise NotImplementedError
