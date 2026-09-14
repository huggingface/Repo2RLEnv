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
from repo2rlenv.quality.loop.models import ProbeManifest
from repo2rlenv.quality.loop.remote import RemoteTrials
from repo2rlenv.quality.loop.runner import QualityLoop
from repo2rlenv.tasksmith.author.artifact import artifact_stage, canonical_digest
from repo2rlenv.tasksmith.author.budget import AuthorBudget
from repo2rlenv.tasksmith.models import Design, Options, Panel, Profile
from repo2rlenv.tasksmith.reuse import generation_task, load_generation, prepared_probe_definitions
from repo2rlenv.tasksmith.source import resolve_pr, validate_source_records


def _validate_profile_source_coverage(profile: Profile, source_files: list[str]) -> None:
    roots = [PurePosixPath(path) for path in profile.options.source_paths]
    uncovered = [
        name
        for name in source_files
        if not any(
            PurePosixPath(name) == root or root in PurePosixPath(name).parents for root in roots
        )
    ]
    if uncovered:
        raise ValueError(
            "options.source_paths omits PR source changes: "
            + ", ".join(uncovered)
            + ". Correct options.source_paths to cover every listed path, keeping source roots "
            "disjoint from options.test_paths and options.public_exclude. Retaining added "
            "example/tool files in the learner checkout does not require selecting or executing "
            "them as tests."
        )
    for name in source_files:
        path = PurePosixPath(name)
        if any(
            path == PurePosixPath(prefix) or PurePosixPath(prefix) in path.parents
            for prefix in profile.options.test_paths + profile.options.public_exclude
        ):
            raise ValueError(f"Profile hides PR source change {name}")


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
                timeout_sec=self.options.worker_timeout_sec,
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
                initial_draft_key=(
                    "previous_design"
                    if role == "design" and inputs.get("previous_design") is not None
                    else None
                ),
            )
        )

    def release_author_worker(self):
        """Stop an unused author worker and reconcile only confirmed Modal usage."""
        if self.worker is not None and self.receipt is None:
            raise ValueError("Cannot release an author worker without its receipt")
        # A controller can resume directly at quality without live worker handles.
        # Persisted receipts remain authoritative for cleanup and pending spend.
        receipts = {
            path
            for path in (self.directory / "workers").glob("*.json")
            if not path.name.endswith(".cost.json")
        }
        if self.receipt is not None:
            receipts.add(self.receipt)
        for receipt in sorted(receipts):
            try:
                record = stop_worker(receipt, self.budget)
            finally:
                if (
                    receipt == self.receipt
                    and json.loads(receipt.read_text()).get("state") == "terminated"
                ):
                    # Never reuse confirmed-dead handles, even if the ledger
                    # update fails. Keep the receipt until reconciliation works.
                    self.worker = self.python = None
            if record["spec"]["provider"] == "modal":
                from repo2rlenv.tasksmith.matrix_runner import reconcile_worker

                operations = {
                    operation["id"]: operation for operation in self.ledger.status()["operations"]
                }
                operation = operations.get(record["operation_id"])
                if operation is None:
                    raise ValueError("Stopped author worker has no budget reservation")
                if operation["status"] != "settled":
                    reconcile_worker(receipt, self.budget)
                self.event(
                    "cleanup", "Author worker terminated and compute reconciled before GPU quality"
                )
            else:
                self.event(
                    "cleanup",
                    "Author worker terminated; provider compute reservation retained for reconciliation",
                )
            if self.receipt == receipt:
                self.receipt = None

    def review_candidate(self, root: Path, source: dict, constructed: dict):
        task = Path(constructed["local"]) / constructed["value"]["task_relative"]
        imported = constructed.get("imported_from", {})
        if self.options.required_probe_focus:
            from repo2rlenv.quality.loop.requirements import with_probe_requirements

            original_task, original_hash = task, task_identity(task)
            task = with_probe_requirements(
                task, root / "quality-input" / task.name, self.options.required_probe_focus
            )
            if task_identity(task) != original_hash:
                # New requirements change the reviewed contract. Retain imported
                # evidence at its original identity and perform fresh checks.
                imported = {}
                save_record(
                    root / "quality-input.json",
                    {
                        "source_task": str(original_task.resolve()),
                        "source_bundle_hash": original_hash,
                        "task_path": str(task.resolve()),
                        "bundle_hash": task_identity(task),
                        "required_probe_focus": self.options.required_probe_focus,
                        "imported_evidence_reused": False,
                    },
                )
        if self.options.gpus:
            # Native quality and its repairs use the downloaded bundle directly.
            # Verify that artifact before releasing the author's remote checkout.
            task_identity(task)
            self.release_author_worker()
        else:
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
                "campaign_design_guidance": source.get("campaign_design_guidance"),
            },
            on_event=lambda event: self.event("quality/" + event.stage, event.message),
        )
        if self.options.gpus:
            from repo2rlenv.quality.loop.native import NativeModalTrials

            loop.remote = NativeModalTrials(output, budget, self.options.quality)
        else:
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
        probes = None
        if imported.get("probes"):
            probes = root / "imported-probes.json"
            save_record(
                probes,
                {
                    "bundle_hash": task_identity(task),
                    "probes": imported["probes"],
                },
            )
        evidence = {}
        for role, trial in imported.get("trials", {}).items():
            if (
                role == "rollout"
                and trial["model"] != self.options.quality.solver_model.qualified_name
            ):
                continue
            evidence[role] = Path(trial["result"])
        result = loop.run(task, probes=probes, resume=(output / "run.json").exists(), **evidence)
        publication = output / "labeled-task.json"
        return {
            "quality": result.model_dump(mode="json"),
            "status": result.status,
            "label_export": json.loads(publication.read_text()) if publication.is_file() else None,
        }

    def _prepared_profile(self, source: dict) -> Profile | None:
        prepared = self.options.prepared_profiles.get(source["id"])
        if prepared is None:
            return None
        validate_source_records([source], [source["url"]])
        binding = {
            **{key: source[key] for key in ("url", "head", "base", "workspace_strategy")},
            "source_diff_sha256": hashlib.sha256(source["source_diff"].encode()).hexdigest(),
        }
        if any(getattr(prepared, key) != value for key, value in binding.items()):
            raise ValueError("Prepared profile differs from the frozen PR source")
        profile = Profile.model_validate(prepared.profile.model_dump())
        if profile.resource != ("gpu" if self.options.gpus else "cpu"):
            raise ValueError("Prepared profile resource differs from the campaign GPU requirement")
        _validate_profile_source_coverage(profile, source["source_files"])
        return profile

    def _prepared_design(self, source: dict) -> Design | None:
        prepared = self.options.prepared_designs.get(source["id"])
        if prepared is None:
            return None
        validate_source_records([source], [source["url"]])
        binding = {
            **{key: source[key] for key in ("url", "head", "base", "workspace_strategy")},
            "source_diff_sha256": hashlib.sha256(source["source_diff"].encode()).hexdigest(),
        }
        if any(getattr(prepared, key) != value for key, value in binding.items()):
            raise ValueError("Prepared design differs from the frozen PR source")
        profile = self._prepared_profile(source)
        if profile is None:
            raise ValueError("A prepared design requires its exact prepared profile")
        if canonical_digest(profile.model_dump(mode="json")) != prepared.profile_sha256:
            raise ValueError("Prepared design differs from the prepared profile digest")
        return Design.model_validate(prepared.design.model_dump())

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
        prepared_profile = self._prepared_profile(source)
        prepared_design = self._prepared_design(source)
        self.event("intake", source["url"])
        inspected = self.remote(root, "inspect", {"stage": "inspect", "source": source})
        if inspected["status"] != "completed":
            raise ValueError(inspected["error"])
        checkout = inspected["value"]["checkout"]
        snapshot_links = inspected["value"].get("snapshot_links", [])
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
                if profile.resource != ("gpu" if self.options.gpus else "cpu"):
                    raise ValueError(
                        "Profile resource must match the campaign's explicit GPU requirement"
                    )
                previous = state.get("profile", {}).get("options", {})
                retained_links = set(previous.get("materialize_document_links", []))
                retained_links.update(
                    link["path"]
                    for link in snapshot_links
                    if PurePosixPath(link["path"]).suffix.lower() in {".md", ".rst", ".txt"}
                )
                if missing_links := retained_links - set(
                    profile.options.materialize_document_links
                ):
                    raise ValueError(
                        "The frozen checkout needs all identified document links before building: "
                        + ", ".join(sorted(missing_links))
                    )
                _validate_profile_source_coverage(profile, source["source_files"])

            if attempt == 0 and prepared_profile is not None:
                asyncio.run(validate(prepared_profile))
                profile = prepared_profile
                self.event("reuse", f"Use prepared profile for {source['id']}; bootstrap required")
            else:
                profile = self.author(
                    root,
                    f"investigate-{attempt}",
                    Profile,
                    {
                        "source": source_context,
                        "snapshot_links": snapshot_links,
                        "previous_profile": state.get("profile"),
                        "previous_failure": state.get("failure"),
                        "repository_bootstrap_hint": hint.model_dump() if hint else None,
                        "requested_resources": {
                            "gpus": self.options.gpus,
                            "gpu_type": "L4" if self.options.gpus else None,
                        },
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
            if self.options.gpus:
                from repo2rlenv.tasksmith.native_stages import NativeStages

                result = NativeStages(self).bootstrap(
                    root,
                    f"bootstrap-{state['profile_attempt']}",
                    source,
                    state["profile"],
                    checkout,
                )
            else:
                result = self.remote(
                    root,
                    f"bootstrap-{state['profile_attempt']}",
                    {
                        "stage": "bootstrap",
                        "source": source,
                        "profile": state["profile"],
                        "checkout": checkout,
                    },
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
            reusable = False
            if attempt == 0 and prepared_design is not None:
                seed = self.options.prepared_designs[source["id"]]
                actual_profile = canonical_digest(
                    Profile.model_validate(state["profile"]).model_dump(mode="json")
                )
                reusable = actual_profile == seed.profile_sha256
                receipt = {
                    "source_id": source["id"],
                    "prepared_design_sha256": canonical_digest(seed.model_dump(mode="json")),
                    "profile_sha256": actual_profile,
                    "readiness_sha256": canonical_digest(state["ready"]),
                    "bootstrap_attempt": state["profile_attempt"],
                    "status": "reused" if reusable else "profile_changed",
                }
                save_record(root / "prepared-design" / f"{canonical_digest(receipt)}.json", receipt)
                self.event(
                    "reuse",
                    f"Use prepared design for {source['id']} after fresh bootstrap"
                    if reusable
                    else f"Prepared design profile changed for {source['id']}; author a new design",
                )
            if reusable:
                value = prepared_design
            else:
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
                        **(
                            {"required_probe_focus": self.options.required_probe_focus}
                            if self.options.required_probe_focus
                            else {}
                        ),
                        "requested_resources": {
                            "gpus": self.options.gpus,
                            "gpu_type": "L4" if self.options.gpus else None,
                        },
                    },
                    checkout,
                )
            return {"design": value.model_dump(), "design_attempt": attempt + 1, "failure": None}

        def construct(state):
            self.event("construct", f"{source['id']} test PR contrast and emit Harbor bundle")
            if self.options.gpus:
                from repo2rlenv.tasksmith.native_stages import NativeStages

                result = NativeStages(self).construct(
                    root,
                    f"construct-{state['design_attempt']}",
                    source,
                    state["profile"],
                    state["design"],
                    state["ready"],
                )
            else:
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
        source_records: list[dict] | None = None,
        prepared_task: Path | None = None,
        prepared_probes: ProbeManifest | None = None,
    ):
        if reuse_evidence and generation_run is None:
            raise ValueError("Evidence reuse requires --generation-run")
        if prepared_probes is not None and prepared_task is None:
            raise ValueError("Prepared probes require a prepared task")
        if source_records is not None:
            if generation_run is not None:
                raise ValueError("Choose frozen source records or generation reuse, not both")
            validate_source_records(source_records, panel.prs)
        if prepared_task is not None:
            if self.options.prepared_designs:
                raise ValueError("Choose a prepared design or generated task for each PR")
            if generation_run is not None or source_records is None or len(source_records) != 1:
                raise ValueError("Prepared task recovery requires exactly one frozen source record")
            source = source_records[0]
            prepared_task = prepared_task.resolve()
            definitions = prepared_probe_definitions(
                prepared_task,
                prepared_probes,
                max_probes=self.options.quality.max_probes,
                required_focus=self.options.required_probe_focus,
            )
            imported = generation_task(
                {
                    "id": source["id"],
                    "quality": {
                        "task_path": str(prepared_task),
                        "bundle_hash": task_identity(prepared_task),
                    },
                },
                source,
                prepared_task.parent,
            )
            if prepared_probes is not None:
                imported["probes"] = definitions
            self.imported = {source["id"]: imported}
        if limit is not None and not 1 <= limit <= len(panel.prs):
            raise ValueError("stop-after must be within the frozen panel size")
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._run(panel, limit, generation_run, reuse_evidence, source_records)

    def _run(self, panel, limit, generation_run, reuse_evidence=False, source_records=None):
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
        if source_records is not None:
            configuration["frozen_sources_digest"] = canonical_digest(source_records)
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            if saved["configuration"] != configuration:
                raise ValueError(
                    "Pilot configuration changed; preserve its receipts and use a new output directory"
                )
            sources = saved["sources"]
        else:
            sources = previous_sources or source_records or [resolve_pr(url) for url in panel.prs]
            save_record(manifest, {"configuration": configuration, "sources": sources})
        if self.options.prepared_profiles:
            selected = {source["id"] for source in sources[:limit]}
            if self.options.prepared_profiles.keys() - selected:
                raise ValueError("Prepared profiles include a PR outside the selected sources")
            if self.options.prepared_profiles.keys() & self.imported.keys():
                raise ValueError("Choose a prepared profile or generated task for each PR")
            # Validate every seed before even the shared dependency preseed can
            # allocate a worker. The checkout inspection still precedes its build.
            for source in sources[:limit]:
                self._prepared_profile(source)
        if self.options.prepared_designs:
            selected = {source["id"] for source in sources[:limit]}
            if self.options.prepared_designs.keys() - selected:
                raise ValueError("Prepared designs include a PR outside the selected sources")
            if self.options.prepared_designs.keys() & self.imported.keys():
                raise ValueError("Choose a prepared design or generated task for each PR")
            for source in sources[:limit]:
                self._prepared_design(source)
        reports = []
        try:
            pending = [
                source
                for source in sources[:limit]
                if not (self.directory / "candidates" / source["id"] / "result.json").is_file()
                and source["id"] not in self.imported
            ]
            unprepared = [
                source for source in pending if source["id"] not in self.options.prepared_profiles
            ]
            for repo in dict.fromkeys(source["repo"] for source in unprepared):
                hint = self.options.bootstrap_hints.get(repo)
                if hint is not None and not self.options.gpus:
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
