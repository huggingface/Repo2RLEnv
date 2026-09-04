from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from repo2rlenv.curation.agent import SHELL_TOOL, run_agent
from repo2rlenv.curation.artifacts import digest_task, finalize, release_task
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.cloud import AuthorSandbox
from repo2rlenv.curation.evaluate import TAMPER, evidence_summary, preflight, trial
from repo2rlenv.curation.models import CampaignConfig, acceptance
from repo2rlenv.curation.prompts import AUTHOR
from repo2rlenv.curation.review import review
from repo2rlenv.curation.sources import resolve_pr

logger = logging.getLogger(__name__)


class CandidateDeferred(RuntimeError):
    """The author found no honest task under the configured resource profile."""


VALIDATE_TOOL = {
    "type": "function",
    "function": {
        "name": "validate_candidate",
        "description": "Export /output/task, validate the spec, build on Modal and run Harbor baseline/oracle. Costs compute; use after authoring complete files.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}

DEFER_TOOL = {
    "type": "function",
    "function": {
        "name": "defer_candidate",
        "description": "Stop this candidate when no substantive, honestly verifiable task fits the CPU/offline profile. Supply concrete evidence and reasons.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
}


def save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


async def curate_one(
    source: dict, root: Path, config: CampaignConfig, budget: Budget, seed_task: Path | None = None
) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    save(root / "source.json", source)
    sandbox = AuthorSandbox(config.author_timeout_sec)
    reserve = budget.reserve(config.author_cloud_allowance_usd, "cloud:author:" + source["id"])
    started = time.monotonic()
    attempt = 0
    cached = {}

    async def defer_candidate(reason: str) -> str:
        save(root / "deferred.json", {"reason": reason, "source": source["url"]})
        raise CandidateDeferred(reason)

    async def validate_candidate() -> str:
        nonlocal attempt
        attempt += 1
        folder = root / "drafts" / str(attempt)
        task = folder / "task"
        await sandbox.export(task)
        try:
            finalize(task, source)
        except ValueError as exc:
            return "Structural validation failed: " + str(exc)
        digest = digest_task(task)
        if digest not in cached or any(t.error for t in cached[digest]):
            logger.info("%s: remote baseline and oracle, draft %s", source["id"], attempt)
            cached[digest] = await preflight(task, folder / "trials", config=config, budget=budget)
        return evidence_summary(cached[digest])

    try:
        await sandbox.start()
        save(
            root / "sandbox.json",
            {"id": sandbox.sandbox.object_id, "timeout_sec": config.author_timeout_sec},
        )
        await sandbox.prepare(source)
        feedback = "Investigate this PR and create the task.\n" + json.dumps(source)
        if seed_task is not None:
            digest_task(seed_task)
            for p in seed_task.rglob("*"):
                if p.is_file():
                    await sandbox.sandbox.filesystem.copy_from_local.aio(
                        p, "/output/task/" + p.relative_to(seed_task).as_posix()
                    )
            feedback += (
                "\nA previous draft is restored in /output/task. Inspect and repair it, "
                "rather than starting over. Previous infrastructure issues have been fixed. "
                "Ensure EVERY dependency has an exact == version; ranges and unversioned "
                "dependencies are rejected before builds. Re-run validate_candidate."
            )
        for revision in range(config.max_revisions):
            logger.info("%s: author revision %s", source["id"], revision + 1)
            await run_agent(
                model=config.author_model,
                system=AUTHOR,
                prompt=feedback,
                budget=budget,
                tools=[SHELL_TOOL, VALIDATE_TOOL, DEFER_TOOL],
                handlers={
                    "shell": sandbox.shell,
                    "validate_candidate": validate_candidate,
                    "defer_candidate": defer_candidate,
                },
                trace=root / f"author-{revision}.jsonl",
                max_turns=config.author_turns,
                max_cost=8,
            )
            folder = root / f"revision-{revision}"
            task = folder / "task"
            await sandbox.export(task)
            try:
                contract = finalize(task, source)
            except ValueError as exc:
                feedback = "Repair the structural validation failure: " + str(exc)
                continue
            digest = digest_task(task)
            trials = list(
                cached.get(digest)
                or await preflight(task, folder / "trials", config=config, budget=budget)
            )
            if (
                len(trials) != 2
                or not all(t.valid for t in trials)
                or trials[0].reward != 0
                or trials[1].reward != 1
            ):
                feedback = "Repair based on these baseline/oracle results:\n" + evidence_summary(
                    trials
                )
                continue
            # Cheap correctness gates precede paid model rollouts.
            for i in range(1, config.oracle_repeats):
                trials.append(
                    await trial(
                        task, folder / "trials", f"oracle-{i}", config=config, budget=budget
                    )
                )
            trials.append(
                await trial(
                    task, folder / "trials", "tamper", config=config, budget=budget, script=TAMPER
                )
            )
            for mutation in contract.mutations:
                trials.append(
                    await trial(
                        task,
                        folder / "trials",
                        f"mutation-{mutation.name}",
                        config=config,
                        budget=budget,
                        script=mutation.script,
                        mutation=True,
                    )
                )
            bad = [
                t
                for t in trials
                if not t.valid or t.reward != (1 if t.label.startswith("oracle") else 0)
            ]
            if bad:
                feedback = "Repair validation; these controls failed:\n" + evidence_summary(bad)
                continue
            for model_index, model in enumerate(config.solver_models):
                for k in range(config.solver_attempts):
                    logger.info("%s: solver %s attempt %s", source["id"], model, k + 1)
                    trials.append(
                        await trial(
                            task,
                            folder / "trials",
                            f"solver-{model_index}-{k}",
                            config=config,
                            budget=budget,
                            model=model,
                        )
                    )
            trials.append(
                await trial(
                    task,
                    folder / "trials",
                    "adversary",
                    config=config,
                    budget=budget,
                    model=config.author_model,
                    adversary=True,
                )
            )
            # Cached preflight evidence is copied into this revision's review tree.
            for t in trials:
                p = Path(t.path)
                if not p.is_relative_to(folder):
                    dest = folder / "trials" / p.name
                    shutil.copytree(p, dest, dirs_exist_ok=True)
                    t.path = str(dest)
            save(
                folder / "evidence.json",
                {"task_digest": digest, "trials": [t.model_dump() for t in trials]},
            )
            logger.info("%s: independent specification and trajectory review", source["id"])
            result = await review(task, folder, trials, model=config.judge_model, budget=budget)
            reasons = acceptance(
                trials, result, config, digest, [m.name for m in contract.mutations]
            )
            verdict = {
                "id": source["id"],
                "source": source["url"],
                "task_digest": digest,
                "status": "accepted" if not reasons else "rejected",
                "score": result.score,
                "reasons": reasons,
                "task_path": str(task),
                "human_review": "pending",
                "review_path": str(folder / "review.json"),
            }
            save(root / "verdict.json", verdict)
            if not reasons:
                return verdict
            feedback = (
                "Repair this task using the independent review, then revalidate.\n"
                + result.model_dump_json()
                + "\nGate failures: "
                + json.dumps(reasons)
            )
        return {
            "id": source["id"],
            "source": source["url"],
            "status": "rejected",
            "reasons": ["Revision limit reached", feedback],
            "human_review": "pending",
        }
    finally:
        try:
            await sandbox.stop()
        finally:
            budget.settle(reserve, 0.15 + (time.monotonic() - started) * 0.00005, estimated=True)


async def campaign(
    seeds: list[str], out: Path, config: CampaignConfig, *, retry_rejected: bool = False
) -> dict:
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / "config.json"
    if (
        config_path.exists()
        and CampaignConfig.model_validate_json(config_path.read_text()) != config
    ):
        raise ValueError(
            "Resume requires the original config; use a new output directory for a changed campaign"
        )
    save(config_path, config.model_dump())
    budget = Budget(out / "budget.json", config.budget_usd)
    manifest_path = out / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {
            "target": config.target,
            "accepted": [],
            "rejected": [],
            "status": "running",
            "human_review": "pending",
            "seeds": seeds,
        }
    )
    if retry_rejected:
        manifest.setdefault("previous_attempts", []).extend(manifest["rejected"])
        manifest["rejected"] = []
    manifest["status"] = "running"
    completed = {v["source"] for v in manifest["accepted"] + manifest["rejected"]}

    async def process(url: str):
        if len(manifest["accepted"]) >= config.target or manifest["status"] == "budget_exhausted":
            return
        if url in completed:
            return
        start = budget.spent
        scoped = Budget(
            budget.path,
            config.budget_usd,
            scope=f"{url}:{time.time_ns()}",
            scope_limit=config.max_candidate_usd,
        )
        logger.info(
            "Curating %s; accepted %s/%s; charged/reserved $%.2f",
            url,
            len(manifest["accepted"]),
            config.target,
            start,
        )
        try:
            source = await asyncio.to_thread(resolve_pr, url)
            parent = out / "candidates" / source["id"]
            previous = sorted(parent.glob("**/task/contract.json"), key=lambda p: p.stat().st_mtime)
            seed_task = previous[-1].parent if previous else None
            candidate_dir = parent / str(time.time_ns())
            result = await curate_one(source, candidate_dir, config, scoped, seed_task)
            if result["status"] == "accepted":
                release_task(Path(result["task_path"]), out / "tasks" / source["id"])
        except CandidateDeferred as exc:
            result = {"source": url, "status": "rejected", "reasons": ["Deferred: " + str(exc)]}
        except BudgetExceeded as exc:
            result = {"source": url, "status": "rejected", "reasons": [str(exc)]}
            if budget.spent + 1 >= config.budget_usd:
                manifest["status"] = "budget_exhausted"
        except Exception as exc:
            logger.exception("Candidate failed: %s", url)
            result = {
                "source": url,
                "status": "rejected",
                "reasons": [type(exc).__name__ + ": " + str(exc)],
            }
        result["charged_or_reserved_usd"] = scoped.spent
        manifest["accepted" if result["status"] == "accepted" else "rejected"].append(result)
        manifest["charged_or_reserved_usd"] = budget.spent
        save(manifest_path, manifest)

    queue = asyncio.Queue()
    for url in dict.fromkeys(seeds):
        queue.put_nowait(url)

    async def worker():
        while not queue.empty():
            url = queue.get_nowait()
            try:
                await process(url)
            finally:
                queue.task_done()

    async with asyncio.TaskGroup() as group:
        for _ in range(config.concurrency):
            group.create_task(worker())
    if len(manifest["accepted"]) >= config.target:
        manifest["status"] = "target_reached"
    elif manifest["status"] == "running":
        manifest["status"] = "seeds_exhausted"
    save(manifest_path, manifest)
    return manifest
