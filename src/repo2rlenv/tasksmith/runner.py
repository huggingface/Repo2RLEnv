"""Resumable LangGraph orchestration over existing owned execution components."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import shlex
import time
from pathlib import Path, PurePosixPath
from typing import TypedDict

from repo2rlenv.auth import resolve_llm_api_key
from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.artifacts import (
    check_runtime_wheel,
    install_runtime,
    runtime_python,
    unpack_evidence,
)
from repo2rlenv.execution.base import WorkerSpec
from repo2rlenv.execution.jobs import launch_job, observe_job
from repo2rlenv.execution.lifecycle import (
    prepare_docker,
    provision_worker,
    save_record,
    stop_worker,
)
from repo2rlenv.quality.loop.artifacts import task_identity
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.quality.loop.remote import RemoteTrials
from repo2rlenv.quality.loop.runner import QualityLoop
from repo2rlenv.tasksmith.author.artifact import artifact_stage, canonical_digest
from repo2rlenv.tasksmith.author.budget import AuthorBudget
from repo2rlenv.tasksmith.models import Design, Options, Panel, Profile
from repo2rlenv.tasksmith.reuse import load_generation
from repo2rlenv.tasksmith.source import resolve_pr


class State(TypedDict, total=False):
    source: dict
    profile: dict
    ready: dict
    design: dict
    constructed: dict
    quality: dict
    profile_attempt: int
    design_attempt: int
    failure: dict | None
    status: str


class Tasksmith:
    def __init__(
        self, directory: Path, campaign: Path, options: Options, wheel: Path, *, on_event=None
    ):
        self.directory, self.options, self.wheel = directory.resolve(), options, wheel.resolve()
        self.ledger = BudgetLedger(campaign / "budget.sqlite3")
        self.prefix = "ts-" + hashlib.sha256(str(self.directory).encode()).hexdigest()[:12]
        self.budget = RunBudget(self.ledger, self.prefix, options.max_spend_usd)
        self.on_event = on_event or (lambda stage, message: None)
        self.worker = self.receipt = self.python = None
        self.imported = {}

    def event(self, stage, message):
        self.on_event(stage, message)
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "events.jsonl").open("a") as stream:
            stream.write(
                json.dumps({"time": time.time(), "stage": stage, "message": message}) + "\n"
            )

    def ready_worker(self):
        if self.worker is not None:
            return
        check_runtime_wheel(self.wheel)
        workers = self.directory / "workers"
        receipts = [path for path in workers.glob("*.json") if not path.name.endswith(".cost.json")]
        index = len(receipts)
        # Never allocate while an older worker's create/termination is unresolved.
        for path in receipts:
            record = json.loads(path.read_text())
            if record["state"] != "terminated":
                raise ValueError(f"Reconcile existing worker before creating another: {path}")
        self.worker, self.receipt = provision_worker(
            WorkerSpec(
                provider=self.options.provider,
                name=f"{self.prefix}-w{index}",
                cpus=self.options.worker_cpus,
                memory_mb=self.options.worker_memory_mb,
                snapshot_id=self.options.worker_snapshot,
                timeout_sec=14400,
            ),
            workers,
            self.budget,
            reserve_usd=self.options.worker_reservation_usd,
        )
        self.event("worker", f"Remote {self.options.provider} worker ready")
        prepare_docker(self.worker)
        self.python = runtime_python(
            install_runtime(self.worker, self.wheel, self.directory / "runtime")
        )

    def remote(self, root: Path, key: str, data: dict) -> dict:
        self.ready_worker()
        # Deterministic build artifacts include worker identity: local image IDs
        # and remote snapshot paths must never be reused on a different worker.
        epoch = hashlib.sha256(self.worker.id.encode()).hexdigest()[:8]
        stage = root / "remote" / f"{key}-{epoch}"
        remote = f"/work/tasksmith/{self.prefix}/{root.name}/{key}"
        identity = canonical_digest(
            {"data": data, "runtime": check_runtime_wheel(self.wheel), "worker": self.worker.id}
        )
        receipt = stage / "operation.json"
        if receipt.exists():
            if json.loads(receipt.read_text())["identity"] != identity:
                raise ValueError("Remote effect inputs changed; preserve it and use a new attempt")
        else:
            stage.mkdir(parents=True, exist_ok=True)
            request = stage / "request.json"
            save_record(request, data)
            self.worker.exec(["mkdir", "-p", remote], timeout=30).checked(
                "Tasksmith remote directory"
            )
            self.worker.upload(request, remote + "/request.json")
            # This marker intentionally precedes the effect. On uncertain launch,
            # resume observes the known supervisor rather than launching twice.
            save_record(
                receipt,
                {
                    "identity": identity,
                    "worker": self.worker.id,
                    "remote": remote,
                    "status": "dispatching",
                },
            )
            launch_job(
                self.worker,
                remote + "/job",
                [
                    self.python,
                    "-m",
                    "repo2rlenv.tasksmith.worker",
                    remote + "/request.json",
                    remote + "/artifact",
                ],
                timeout_sec=1500,
                python=self.python,
            )
        local = stage / "artifact"
        if not local.exists():
            job = observe_job(self.worker, remote + "/job", timeout_sec=1600)
            if job["state"] != "completed":
                raise RuntimeError(f"Remote supervisor failed; inspect {remote}/job")
            archive = stage / "artifact.tar.gz"
            self.worker.download(remote + "/artifact.tar.gz", archive)
            unpack_evidence(archive, stage, root_name="artifact")
        result = json.loads((local / "stage-result.json").read_text())
        result["local"] = str(local)
        save_record(
            receipt,
            {
                "identity": identity,
                "worker": self.worker.id,
                "remote": remote,
                "status": "completed",
                "result": result,
            },
        )
        return result

    def author(self, root, stage, schema, inputs, checkout, *, validate=None):
        async def shell(command: str, timeout_sec: int = 120):
            if not isinstance(timeout_sec, int) or not 1 <= timeout_sec <= 120:
                raise ValueError("Shell timeout must be 1-120 seconds")
            result = await asyncio.to_thread(
                self.worker.exec,
                ["sh", "-c", "cd " + shlex.quote(checkout) + " && " + command],
                timeout=timeout_sec,
            )
            return json.dumps(
                {
                    "returncode": result.returncode,
                    "stdout": result.stdout[:22000],
                    "stderr": result.stderr[:6000],
                }
            )

        path = root / "authors" / stage
        role = stage.split("-", 1)[0]
        prompt = (Path(__file__).parent / "prompts" / (role + ".md")).read_text()
        prompt += (
            f"\nThis stage has at most {self.options.author_turns} model calls. "
            "Batch related reads and submit as soon as the evidence is sufficient. "
            "The final two calls are reserved for submission and schema correction; "
            "shell exploration is disabled then.\n"
        )
        return asyncio.run(
            artifact_stage(
                schema=schema,
                stage=stage,
                inputs=inputs,
                system=prompt,
                prompt=json.dumps({"checkout": checkout, **inputs}, indent=2),
                root=path,
                budget=AuthorBudget(self.budget, path / "charges", root.name + "-" + stage),
                model=self.options.author_model,
                runtime=self.options.author_runtime,
                max_cost=float(self.options.author_stage_usd),
                max_turns=self.options.author_turns,
                deadline=time.time() + 1200,
                shell=shell,
                validate=validate,
            )
        )

    def review_candidate(self, root: Path, source: dict, constructed: dict):
        self.ready_worker()
        self.event(
            "quality",
            f"{source['id']} review, controls, probes and {self.options.quality.solver_model.qualified_name} rollout",
        )
        output = root / "quality"
        budget = RunBudget(
            self.budget,
            f"{self.prefix}-q-{source['id'][:8]}",
            self.options.quality.max_spend_usd,
        )
        loop = QualityLoop(
            self.options.quality,
            output,
            self.ledger,
            budget=budget,
            protected_paths=("solution", "environment/source"),
            task_context={
                "kind": "merged_pr",
                "title": source.get("title", ""),
                "body": source.get("body", ""),
                "source_diff": source.get("source_diff", "")[:24000],
                "source_diff_truncated": len(source.get("source_diff", "")) > 24000,
                "reference_policy": "fixed_pr_head",
            },
            on_event=lambda event: self.event("quality/" + event.stage, event.message),
        )
        loop.remote = RemoteTrials(
            output,
            budget,
            self.options.quality,
            wheel=self.wheel,
            provider=self.options.provider,
            worker_receipt=self.receipt,
        )
        # Reuse the already prepared worker/runtime, avoiding redundant setup.
        loop.remote.worker, loop.remote.python = self.worker, self.python
        task = Path(constructed["local"]) / constructed["value"]["task_relative"]
        probes = None
        if constructed.get("imported_from", {}).get("probes"):
            probes = root / "imported-probes.json"
            save_record(
                probes,
                {
                    "bundle_hash": task_identity(task),
                    "probes": constructed["imported_from"]["probes"],
                },
            )
        evidence = {}
        for role, trial in constructed.get("imported_from", {}).get("trials", {}).items():
            if (
                role == "rollout"
                and trial["model"] != self.options.quality.solver_model.qualified_name
            ):
                continue
            evidence[role] = Path(trial["result"])
        result = loop.run(task, probes=probes, resume=(output / "run.json").exists(), **evidence)
        return {"quality": result.model_dump(mode="json"), "status": result.status}

    def candidate(self, source: dict):
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, START, StateGraph

        root = self.directory / "candidates" / source["id"]
        root.mkdir(parents=True, exist_ok=True)
        result_path = root / "result.json"
        if result_path.exists():
            saved = json.loads(result_path.read_text())
            quality = saved.get("quality", {})
            if quality and task_identity(Path(quality["task_path"])) != quality["bundle_hash"]:
                raise ValueError("Completed task changed; cannot reuse its quality result")
            return saved
        if source["id"] in self.imported:
            imported = self.imported[source["id"]]
            task = Path(imported["task"])
            if task_identity(task) != imported["bundle_hash"]:
                raise ValueError("Imported generation changed after preflight")
            self.event("reuse", f"Reuse generated task for {source['url']}")
            constructed = {
                "local": str(task.parent),
                "value": {"task_relative": task.name},
                "imported_from": imported,
            }
            report = {
                "source": source,
                "id": source["id"],
                "constructed": constructed,
                **self.review_candidate(root, source, constructed),
            }
            save_record(result_path, report)
            return report
        self.event("intake", source["url"])
        inspected = self.remote(root, "inspect", {"stage": "inspect", "source": source})
        if inspected["status"] != "completed":
            raise ValueError(inspected["error"])
        checkout = inspected["value"]["checkout"]
        # Avoid giving every author the full unrelated diff repeatedly; the
        # complete original remains in the frozen source artifact.
        source_context = {key: value for key, value in source.items() if key != "full_diff"}
        source_context["full_pr_diff"] = source.get("full_diff", "")[:32000]
        source_context["full_pr_diff_truncated"] = len(source.get("full_diff", "")) > 32000

        def investigate(state):
            attempt = state.get("profile_attempt", 0)
            hint = self.options.bootstrap_hints.get(source.get("repo", ""))
            self.event(
                "investigate", f"{source['id']} dependency/test profile, attempt {attempt + 1}"
            )

            async def validate(profile):
                roots = [PurePosixPath(path) for path in profile.options.source_paths]
                for name in source["source_files"]:
                    path = PurePosixPath(name)
                    if not any(path == prefix or prefix in path.parents for prefix in roots):
                        raise ValueError(f"Profile omits PR source change {name}")
                    if any(
                        path == PurePosixPath(prefix) or PurePosixPath(prefix) in path.parents
                        for prefix in profile.options.test_paths + profile.options.public_exclude
                    ):
                        raise ValueError(f"Profile hides PR source change {name}")

            profile = self.author(
                root,
                f"investigate-{attempt}",
                Profile,
                {
                    "source": source_context,
                    "previous_profile": state.get("profile"),
                    "previous_failure": state.get("failure"),
                    "repository_bootstrap_hint": hint.model_dump() if hint else None,
                },
                checkout,
                validate=validate,
            )
            return {
                "profile": profile.model_dump(),
                "profile_attempt": attempt + 1,
                "failure": None,
            }

        def bootstrap(state):
            self.event("bootstrap", f"{source['id']} build and offline merged-head readiness")
            result = self.remote(
                root,
                f"bootstrap-{state['profile_attempt']}",
                {"stage": "bootstrap", "source": source, "profile": state["profile"]},
            )
            if result["status"] != "completed":
                return {"failure": result, "status": "bootstrap_failed"}
            cached = result["value"]["dependency_cache"]
            self.event("bootstrap", f"Dependency image cache hit: {cached['cache_hit']}")
            return {"ready": result["value"], "failure": None, "status": "ready"}

        def design(state):
            attempt = state.get("design_attempt", 0)
            self.event(
                "design", f"{source['id']} request and verification design, attempt {attempt + 1}"
            )
            value = self.author(
                root,
                f"design-{attempt}",
                Design,
                {
                    "source": source_context,
                    "profile": state["profile"],
                    "readiness": state["ready"]["readiness"],
                    "previous_design": state.get("design"),
                    "previous_failure": state.get("failure"),
                },
                checkout,
            )
            return {"design": value.model_dump(), "design_attempt": attempt + 1, "failure": None}

        def construct(state):
            self.event("construct", f"{source['id']} test PR contrast and emit Harbor bundle")
            result = self.remote(
                root,
                f"construct-{state['design_attempt']}",
                {
                    "stage": "construct",
                    "source": source,
                    "profile": state["profile"],
                    "design": state["design"],
                    "ready": state["ready"],
                },
            )
            if result["status"] != "completed":
                return {"failure": result, "status": "construction_failed"}
            return {"constructed": result, "failure": None, "status": "generated"}

        def quality(state):
            return self.review_candidate(root, source, state["constructed"])

        graph = StateGraph(State)
        for name, node in (
            ("investigate", investigate),
            ("bootstrap", bootstrap),
            ("design", design),
            ("construct", construct),
            ("quality", quality),
        ):

            def tracked(state, *, operation=node, label=name):
                save_record(
                    root / "progress.json", {**state, "stage": label, "stage_status": "running"}
                )
                update = operation(state)
                save_record(
                    root / "progress.json",
                    {**state, **update, "stage": label, "stage_status": "completed"},
                )
                return update

            graph.add_node(name, tracked)
        graph.add_edge(START, "investigate")
        graph.add_edge("investigate", "bootstrap")
        graph.add_conditional_edges(
            "bootstrap",
            lambda state: (
                (
                    "investigate"
                    if state["profile_attempt"] < self.options.max_stage_attempts
                    else END
                )
                if state["failure"]
                else "design"
            ),
        )
        graph.add_edge("design", "construct")
        graph.add_conditional_edges(
            "construct",
            lambda state: (
                ("design" if state["design_attempt"] < self.options.max_stage_attempts else END)
                if state["failure"]
                else "quality"
            ),
        )
        graph.add_edge("quality", END)
        with SqliteSaver.from_conn_string(str(root / "graph.sqlite3")) as saver:
            # Re-entering from START validates durable stage identities and
            # rematerializes remote-only state if the previous worker expired.
            state = graph.compile(checkpointer=saver).invoke(
                {"source": source, "profile_attempt": 0, "design_attempt": 0, "failure": None},
                {"configurable": {"thread_id": source["id"]}, "recursion_limit": 40},
            )
        report = {"source": source["url"], "id": source["id"], **state}
        save_record(result_path, report)
        return report

    def run(
        self,
        panel: Panel,
        *,
        limit: int | None = None,
        generation_run: Path | None = None,
        reuse_evidence: bool = False,
    ):
        if reuse_evidence and generation_run is None:
            raise ValueError("Evidence reuse requires --generation-run")
        if limit is not None and not 1 <= limit <= len(panel.prs):
            raise ValueError("stop-after must be within the frozen panel size")
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._run(panel, limit, generation_run, reuse_evidence)

    def _run(self, panel, limit, generation_run, reuse_evidence=False):
        # Fail before provisioning if optional orchestration or credentials are absent.
        from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: F401
        from langgraph.graph import StateGraph  # noqa: F401

        from repo2rlenv.tasksmith.author.external_agent import runtime_path

        if not resolve_llm_api_key("anthropic", None):
            raise ValueError(
                "Tasksmith authoring requires ANTHROPIC_API_KEY before worker creation"
            )
        for model in (
            self.options.quality.review_model,
            self.options.quality.repair_model,
            self.options.quality.solver_model,
            self.options.quality.escalation_model,
        ):
            if model is not None and not resolve_llm_api_key(model.provider, model.api_key_env):
                raise ValueError(f"No API key for configured quality provider {model.provider}")
        previous_sources = None
        if generation_run is not None:
            previous_sources, self.imported = load_generation(
                generation_run, panel, reuse_evidence=reuse_evidence
            )
        runtime_path(self.options.author_runtime)
        check_runtime_wheel(self.wheel)
        manifest = self.directory / "panel.json"
        configuration = {
            "reuse_evidence": reuse_evidence,
            "generation_inputs": self.imported,
            "panel": panel.model_dump(),
            "options": self.options.model_dump(mode="json"),
            "ledger": str(self.ledger.path.resolve()),
            "runtime_sha256": check_runtime_wheel(self.wheel),
        }
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            if saved["configuration"] != configuration:
                raise ValueError(
                    "Pilot configuration changed; preserve its receipts and use a new output directory"
                )
            sources = saved["sources"]
        else:
            sources = previous_sources or [resolve_pr(url) for url in panel.prs]
            save_record(manifest, {"configuration": configuration, "sources": sources})
        reports = []
        try:
            pending = [
                source
                for source in sources[:limit]
                if not (self.directory / "candidates" / source["id"] / "result.json").is_file()
                and source["id"] not in self.imported
            ]
            for repo in dict.fromkeys(source["repo"] for source in pending):
                hint = self.options.bootstrap_hints.get(repo)
                if hint is not None:
                    key = hashlib.sha256(repo.encode()).hexdigest()[:12]
                    self.event("cache", f"Prepare recorded source-free dependencies for {repo}")
                    result = self.remote(
                        self.directory / "dependency-seeds",
                        key,
                        {"stage": "dependencies", "hint": hint.model_dump()},
                    )
                    self.event("cache", f"{repo}: {result['status']}")
            for source in sources[:limit]:
                try:
                    reports.append(self.candidate(source))
                except BaseException as exc:
                    progress = self.directory / "candidates" / source["id"] / "progress.json"
                    partial = json.loads(progress.read_text()) if progress.is_file() else {}
                    failure = {
                        **partial,
                        "id": source["id"],
                        "source": source["url"],
                        "status": "incomplete",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    save_record(
                        self.directory / "candidates" / source["id"] / "interruption.json", failure
                    )
                    reports.append(failure)
                    self.event("incomplete", failure["error"])
                    if not isinstance(exc, Exception):
                        raise
                self.report(panel, reports)
        finally:
            if self.receipt is not None:
                stop_worker(self.receipt, self.budget)
                self.event(
                    "cleanup",
                    "Worker terminated; compute reservation retained pending reconciliation",
                )
            self.report(panel, reports)
        return self.report(panel, reports)

    def report(self, panel, reports):
        result = {
            "panel": panel.name,
            "inputs": len(panel.prs),
            "attempted": len(reports),
            "generated": sum(bool(row.get("constructed")) for row in reports),
            "usable": sum(row["status"] == "usable" for row in reports),
            "candidates": reports,
            **self.budget.totals(),
        }
        save_record(self.directory / "report.json", result)
        return result
