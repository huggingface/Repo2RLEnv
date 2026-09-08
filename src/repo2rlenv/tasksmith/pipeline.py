"""Durable Tasksmith stage graph; one retained lineage per input PR."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from repo2rlenv.curation.sources import api, resolve_pr
from repo2rlenv.tasksmith.build import construct, discover
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.models import Deadline, LedgerRef, OperationKey, PRIdentity
from repo2rlenv.tasksmith.state import Journal, ReconciliationRequired
from repo2rlenv.tasksmith.worker import canonical_digest, save_json
from repo2rlenv.ui import console


class PipelineState(TypedDict, total=False):
    source: dict
    discovery: dict
    construction: dict
    validation: dict
    revision: int
    revision_digest: str
    repair: dict | None
    status: str


def freeze_panel(path: Path, root: Path) -> list[dict]:
    """Freeze pins before paid effects; research entries are checked again remotely."""
    frozen = root / "panel.json"
    input_digest = canonical_digest(json.loads(path.read_text()))
    if frozen.exists():
        retained = json.loads(frozen.read_text())
        if retained["input_digest"] != input_digest:
            raise ValueError("The fixed input panel changed; preserve the original denominator")
        return retained["sources"]
    raw = json.loads(path.read_text())
    rows = raw["panel"] if isinstance(raw, dict) else raw
    sources = []
    for row in rows:
        if isinstance(row, str):
            source = resolve_pr(row)
            source.update(
                base_tree_sha=api(f"repos/{source['repo']}/git/commits/{source['base_sha']}")[
                    "tree"
                ]["sha"],
                head_tree_sha=api(f"repos/{source['repo']}/git/commits/{source['head_sha']}")[
                    "tree"
                ]["sha"],
            )
        else:
            identity = PRIdentity.from_url(row["url"])
            source = {**row, "repo": identity.repository, "number": identity.number}
            source["id"] = row.get(
                "id", identity.repository.replace("/", "-") + f"-{identity.number}"
            )
            pr_path = row.get("source_evidence", {}).get("pr")
            if pr_path and Path(pr_path).is_file():
                source["body"] = json.loads(Path(pr_path).read_text()).get("body", "")
        identity = PRIdentity.from_url(source["url"])
        if identity.repository != source["repo"]:
            raise ValueError("PR/repository identity mismatch")
        if identity.key in {PRIdentity.from_url(s["url"]).key for s in sources}:
            raise ValueError("Duplicate PR input")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source["id"]) or source["id"] in {
            s["id"] for s in sources
        }:
            raise ValueError("Panel source IDs must be unique safe directory names")
        for key in ("base_sha", "head_sha", "base_tree_sha", "head_tree_sha"):
            if not re.fullmatch(r"[a-f0-9]{40}", source[key]):
                raise ValueError("Panel needs full immutable commit and tree identities")
        sources.append(source)
    if not sources:
        raise ValueError("Empty PR panel")
    save_json(frozen, {"input_digest": input_digest, "sources": sources})
    return sources


class Candidate:
    def __init__(self, config: TasksmithConfig, source: dict, root: Path, campaign_root: Path):
        self.config, self.source, self.root, self.campaign_root = (
            config,
            source,
            root,
            campaign_root,
        )
        self.pr = PRIdentity.from_url(source["url"])
        self.journal = Journal(campaign_root / "journal.sqlite")
        self.lease = None
        root.mkdir(parents=True, exist_ok=True)
        identity_path = root / "identity.json"
        if identity_path.exists():
            identity = json.loads(identity_path.read_text())
            if identity["config_digest"] != config.digest() or identity[
                "source_digest"
            ] != canonical_digest(source):
                raise ValueError("Candidate configuration or frozen source changed")
        else:
            identity = {
                "config_digest": config.digest(),
                "source_digest": canonical_digest(source),
                "deadline": time.time() + config.candidate_timeout_sec,
            }
            save_json(identity_path, identity)
        self.deadline = identity["deadline"]
        self.journal.register_pr(
            self.pr,
            deadline=Deadline(expires_at=self.deadline),
            ledger=LedgerRef(
                path=str(config.ledger_path),
                scope=f"{config.campaign_id}:{source['id']}",
                group=config.campaign_id,
            ),
        )

    async def effect(self, state: PipelineState, stage: str, inputs: dict, execute):
        key = OperationKey(
            pr_id=self.pr.key,
            revision_digest=state["revision_digest"],
            stage=stage,
            input_digest=canonical_digest(inputs),
            policy_digest=self.config.digest(),
            attempt=0,
            effect=stage,
        )
        # Construction commits its child revision before LangGraph persists the
        # node output. After a crash in that interval the checkpoint still names
        # the parent: recover its exact completed effect before asking the
        # journal to authorize a *new* effect on the current revision.
        with self.journal._transaction() as connection:
            self.journal._lease(connection, self.lease)
        retained = {
            record.key.operation_id: record for record in self.journal.operations(self.pr.key)
        }
        retry_of = None
        while (prior := retained.get(key.operation_id)) is not None:
            if prior.key != key:
                raise ReconciliationRequired("Retained stage key does not match exact inputs")
            if prior.status == "completed" and "result" in prior.artifacts:
                for reference in prior.artifacts.values():
                    self.journal.verify_artifact(reference)
                self.active_attempt = key.attempt
                return json.loads(
                    self.journal.verify_artifact(prior.artifacts["result"]).read_text()
                )
            if prior.status == "failed" and prior.receipt.get("retry_authorized") is True:
                retry_of = key.operation_id
                key = key.model_copy(update={"attempt": key.attempt + 1})
                continue
            raise ReconciliationRequired(
                f"{stage}: retained {prior.status} effect {key.operation_id}; inspect original receipts before retry"
            )
        claim = self.journal.claim_operation(self.lease, key, retry_of=retry_of)
        if not claim.acquired:
            record = claim.operation
            if record.status == "completed" and "result" in record.artifacts:
                for reference in record.artifacts.values():
                    self.journal.verify_artifact(reference)
                self.active_attempt = key.attempt
                return json.loads(
                    self.journal.verify_artifact(record.artifacts["result"]).read_text()
                )
            raise ReconciliationRequired(
                f"{stage}: retained {record.status} effect {key.operation_id}; inspect original receipts before retry"
            )
        self.active_attempt = key.attempt
        try:
            result = await execute()
            artifact = self.journal.commit_artifact(
                self.lease,
                key.operation_id,
                name="result",
                data=json.dumps(result, sort_keys=True, allow_nan=False).encode(),
                media_type="application/json",
            )
            self.journal.finish_operation(
                self.lease,
                key.operation_id,
                receipt={"result": artifact.model_dump(mode="json"), "stage_root": str(self.root)},
            )
            return result
        except BaseException as exc:
            self.journal.mark_uncertain(
                self.lease,
                key.operation_id,
                reason=f"{type(exc).__name__}: {exc}",
                receipt={"stage_root": str(self.root)},
            )
            raise

    async def discover_node(self, state: PipelineState):
        console.info(f"Tasksmith {self.source['id']}: understanding PR and dependency needs")
        result = await self.effect(
            state,
            "discovery",
            self.source,
            lambda: discover(
                self.config,
                self.source,
                self.root
                / (
                    f"discovery-attempt-{self.active_attempt}"
                    if self.active_attempt
                    else "discovery"
                ),
                self.deadline,
            ),
        )
        return {"discovery": result, "status": "understood"}

    async def construct_node(self, state: PipelineState):
        revision = state.get("revision", 0)
        console.info(
            f"Tasksmith {self.source['id']}: bootstrapping and constructing revision {revision}"
        )
        prior = {}
        inputs = {"discovery": state["discovery"], "repair": state.get("repair")}
        if revision > 0:
            prior = {
                "prior_construction": state["construction"],
                "parent_task_digest": state["revision_digest"],
            }
            # The operation key already binds the parent task revision. The
            # construction worker separately binds the verified prior manifest
            # and design; keep the controller's existing repair identity stable
            # so an explicitly reconciled interrupted repair can resume.

        async def execute():
            suffix = f"-attempt-{self.active_attempt}" if self.active_attempt else ""
            result = await construct(
                self.config,
                self.source,
                state["discovery"],
                self.root / f"revision-{revision}{suffix}",
                self.campaign_root / "bootstrap-cache",
                self.deadline,
                state.get("repair"),
                **prior,
            )
            return {
                **result,
                "task_revision": revision,
                "parent_task_digest": state["revision_digest"] if revision > 0 else None,
            }

        result = await self.effect(
            state,
            "construction",
            inputs,
            execute,
        )
        result = {
            **result,
            "task_revision": revision,
            "parent_task_digest": state["revision_digest"] if revision > 0 else None,
        }
        task_digest = result["emitter"]["task_digest"]
        self.journal.register_revision(
            self.lease,
            revision=revision + 1,
            task_digest=task_digest,
            parent_digest=state["revision_digest"],
            manifest=result["emitter"],
        )
        return {"construction": result, "revision_digest": task_digest, "status": "constructed"}

    async def validate_node(self, state: PipelineState):
        from repo2rlenv.tasksmith.validation import validate_candidate

        console.info(f"Tasksmith {self.source['id']}: independent review and remote validation")
        result = await self.effect(
            state,
            "validation",
            {"task": state["revision_digest"]},
            lambda: validate_candidate(
                self.config,
                self.source,
                state["construction"],
                Path(state["construction"]["task_path"]).parent
                / (
                    f"validation-attempt-{self.active_attempt}"
                    if self.active_attempt
                    else "validation"
                ),
                self.deadline,
                self.journal,
                self.lease,
                self.campaign_root,
            ),
        )
        return {"validation": result, "status": result["status"]}

    def route(self, state: PipelineState):
        if state["status"] == "accepted":
            return END
        if state.get("revision", 0) + 1 >= self.config.max_revisions:
            return END
        if state["validation"].get("repairable") and self.deadline > time.time() + 600:
            budget = self.config.budget(self.source["id"])
            if budget.spent + self.config.validation_reserve_usd < self.config.candidate_limit_usd:
                return "repair"
        return END

    async def repair_node(self, state: PipelineState):
        return {
            "revision": state.get("revision", 0) + 1,
            "repair": state["validation"],
            "status": "repairing",
        }

    async def _request_revision(self, app, cfg, snapshot, request: dict):
        """Record new repair evidence without replacing a completed stage result.

        Used when a controller defect is diagnosed after a candidate has stopped.
        The fresh revision still traverses every admission gate, under the same
        deadline and budget. Replaying the same request only resumes its work.
        """
        if set(request) != {"parent_task_digest", "reason", "evidence", "feedback"}:
            raise ValueError("Revision request needs parent digest, reason, evidence and feedback")
        if not all(
            isinstance(request[k], str) and request[k].strip() for k in ("reason", "evidence")
        ):
            raise ValueError("Revision request needs concrete diagnosis and retained evidence")
        if (
            not isinstance(request["feedback"], dict)
            or request["feedback"].get("repairable") is not True
        ):
            raise ValueError("Revision request must contain repairable feedback")
        if request["feedback"].get("status") != "needs_repair":
            raise ValueError("Revision requests can only ask for repair, never acceptance")
        digest = canonical_digest(request)
        state = snapshot.values
        if (state.get("repair") or {}).get("revision_request_digest") == digest:
            return
        # A crash after checkpointing the feedback but before repair_node runs
        # must resume that exact request rather than schedule a second revision.
        if state.get("validation", {}).get("revision_request_digest") == digest:
            return
        if snapshot.next or not state.get("construction"):
            raise ReconciliationRequired(
                "Finish or reconcile the active stage before requesting a revision"
            )
        if any(
            record.status in {"claimed", "submitted", "uncertain"}
            for record in self.journal.operations(self.pr.key)
        ):
            raise ReconciliationRequired(
                "Reconcile unfinished child operations before requesting a revision"
            )
        if state.get("status") == "accepted":
            raise ValueError("An accepted candidate cannot be rerolled by a repair request")
        if request["parent_task_digest"] != state.get("revision_digest"):
            raise ValueError("Revision request does not name the current immutable task")
        previous_validation = state.get("validation", {})
        prior_trials = list(previous_validation.get("all_trials", {}).values())
        if previous_validation.get("outcome"):
            prior_trials.append(previous_validation["outcome"])
        if any(row.get("cleanup_confirmed") is not True for row in prior_trials):
            raise ReconciliationRequired(
                "Reconcile prior trial resources before requesting a revision"
            )
        feedback = {
            **request["feedback"],
            "revision_request_digest": digest,
            "diagnosis": request["reason"],
            "retained_evidence": request["evidence"],
        }
        if self.route({**state, "status": "needs_repair", "validation": feedback}) != "repair":
            raise ValueError("Original revision, deadline or budget limit prevents another repair")

        async def retain_request():
            return request

        await self.effect(state, "revision_request", request, retain_request)
        await app.aupdate_state(
            cfg, {"validation": feedback, "status": "needs_repair"}, as_node="validate"
        )

    async def run(self, *, revision_request: dict | None = None):
        self.lease = self.journal.acquire_lease(
            self.pr.key, owner=f"{os.getpid()}:{uuid4().hex}", ttl_seconds=300
        )
        self.journal.recover_artifacts(self.lease)
        initial_digest = canonical_digest(self.source)
        self.journal.register_revision(
            self.lease,
            revision=0,
            task_digest=initial_digest,
            parent_digest=None,
            manifest={"input": self.source},
        )

        async def renew():
            while True:
                await asyncio.sleep(60)
                self.lease = self.journal.renew_lease(self.lease, ttl_seconds=300)

        heartbeat = asyncio.create_task(renew())
        try:
            graph = StateGraph(PipelineState)
            for name in ("discover", "construct", "validate", "repair"):
                graph.add_node(name, getattr(self, name + "_node"))
            graph.add_edge(START, "discover")
            graph.add_edge("discover", "construct")
            graph.add_edge("construct", "validate")
            graph.add_conditional_edges("validate", self.route)
            graph.add_edge("repair", "construct")
            async with AsyncSqliteSaver.from_conn_string(str(self.root / "graph.sqlite")) as saver:
                app = graph.compile(checkpointer=saver)
                cfg = {"configurable": {"thread_id": self.pr.key}, "recursion_limit": 30}
                snapshot = await app.aget_state(cfg)
                if revision_request is not None:
                    await self._request_revision(app, cfg, snapshot, revision_request)
                    snapshot = await app.aget_state(cfg)
                inputs = (
                    None
                    if snapshot.values
                    else {
                        "source": self.source,
                        "revision": 0,
                        "revision_digest": initial_digest,
                        "repair": None,
                        "status": "started",
                    }
                )
                result = await app.ainvoke(inputs, cfg)
                save_json(self.root / "result.json", result)
                return result
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self.journal.release_lease(self.lease)


async def run_panel(config: TasksmithConfig, panel_path: Path, root: Path) -> dict:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    sources = await asyncio.to_thread(freeze_panel, panel_path, root)
    frozen_config = root / "protocol.json"
    if frozen_config.exists():
        if json.loads(frozen_config.read_text()) != config.model_dump(mode="json"):
            raise ValueError("Campaign protocol changed; do not reset budget/limits on resume")
    else:
        save_json(frozen_config, config.model_dump(mode="json"))
    results = []
    # CPU POC is deliberately sequential until one complete environment proves the stack.
    for source in sources:
        candidate = Candidate(config, source, root / "candidates" / source["id"], root)
        try:
            state = await candidate.run()
            results.append(
                {
                    "id": source["id"],
                    "status": state["status"],
                    "revision": state.get("revision", 0),
                    "result": str(candidate.root / "result.json"),
                }
            )
        except Exception as exc:
            error = {
                "id": source["id"],
                "status": "incomplete",
                "error": f"{type(exc).__name__}: {exc}",
            }
            save_json(candidate.root / "interruption.json", error)
            console.warn(f"Tasksmith {source['id']}: {error['error']}")
            results.append(error)
        report = {
            "inputs": len(sources),
            "accepted": sum(r["status"] == "accepted" for r in results),
            "results": results,
            "remaining_inputs": len(sources) - len(results),
            "spend": config.spend_summary(),
        }
        save_json(root / "report.json", report)
    return report
