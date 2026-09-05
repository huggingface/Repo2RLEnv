from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path

from repo2rlenv.curation.agent import SHELL_TOOL, run_agent
from repo2rlenv.curation.artifacts import digest_task, finalize, release_task
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.cloud import AuthorSandbox
from repo2rlenv.curation.evaluate import (
    TAMPER,
    evidence_summary,
    inspect_execution,
    preflight,
    pytest_tamper,
    trial,
)
from repo2rlenv.curation.inference import inference_digest
from repo2rlenv.curation.models import (
    CampaignConfig,
    Contract,
    Review,
    TrialEvidence,
    acceptance,
    execution_gate_reasons,
)
from repo2rlenv.curation.prompts import AUTHOR
from repo2rlenv.curation.review import review
from repo2rlenv.curation.sources import resolve_pr

logger = logging.getLogger(__name__)
ADMISSION_VERSION = 4


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


def _regular_tree(path: Path) -> None:
    """Reject links and special files before copying retained sandbox evidence."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Not a regular evidence directory: {path}")
    for entry in path.rglob("*"):
        mode = entry.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError(f"Non-regular evidence entry: {entry}")


def _evidence_record(
    digest: str,
    trials: list[TrialEvidence],
    resumed_from: str | None,
    recovery: dict | None = None,
) -> dict:
    data = {
        "admission_version": ADMISSION_VERSION,
        "task_digest": digest,
        "trials": [t.model_dump() for t in trials],
    }
    if resumed_from is not None:
        data["resumed_from"] = resumed_from
    if recovery is not None:
        data["recovery"] = recovery
    return data


def _trial_plan(contract: Contract, config: CampaignConfig) -> list[tuple[str, int | None, dict]]:
    plan = [("baseline", 0, {"script": "true"})]
    plan.extend((f"oracle-{i}", 1, {}) for i in range(config.oracle_repeats))
    plan.append(("tamper", 0, {"script": TAMPER}))
    plan.extend(
        (f"mutation-{m.name}", 0, {"script": m.script, "mutation": True})
        for m in contract.mutations
    )
    plan.append(("pytest-tamper", 0, {"script": pytest_tamper(contract.source_paths)}))
    plan.extend(
        (f"equivalent-{e.name}", 1, {"script": e.script, "mutation": True})
        for e in contract.equivalents
    )
    plan.extend(
        (f"solver-{i}-{k}", None, {"model": model})
        for i, model in enumerate(config.solver_models)
        for k in range(config.solver_attempts)
    )
    plan.append(("adversary", 0, {"model": config.author_model, "adversary": True}))
    return plan


def _prepare_pending_review(
    seed_task: Path, root: Path, source: dict, config: CampaignConfig
) -> tuple[Contract, str, list[TrialEvidence], dict] | None:
    """Copy an unjudged, unchanged task and reusable completed trial evidence."""
    try:
        if seed_task.is_symlink() or not seed_task.is_dir():
            return None
        previous = seed_task.parent.resolve()
        folder = root.resolve() / "revision-0"
        archive = root.resolve() / "prior-review"
        if folder.exists() or archive.exists() or root.resolve().is_relative_to(previous):
            return None
        prior_review = previous / "review.json"
        if prior_review.is_symlink():
            return None
        if prior_review.exists():
            try:
                Review.model_validate_json(prior_review.read_text())
            except ValueError:
                pass  # An interrupted or malformed response has no valid verdict.
            else:
                return None  # A valid quality rejection must not be rerolled.
        _regular_tree(previous)
        digest = digest_task(seed_task)
        evidence_path = previous / "evidence.json"
        candidates = []
        if evidence_path.exists():
            evidence = json.loads(evidence_path.read_text())
            if not isinstance(evidence, dict) or evidence["task_digest"] != digest:
                return None
            # Legacy v3 omitted this field; the current wrapper still must match.
            if evidence.get("admission_version") not in (None, 3, ADMISSION_VERSION):
                return None
            candidates = [TrialEvidence.model_validate(t) for t in evidence["trials"]]
            if len({t.label for t in candidates}) != len(candidates):
                return None
            if any(t.task_digest != digest for t in candidates):
                return None
        known_labels = {t.label for t in candidates}
        for path in sorted((previous / "trials").glob("*.json")):
            try:
                saved = TrialEvidence.model_validate_json(path.read_text())
                if saved.label not in known_labels:
                    candidates.append(saved)
                    known_labels.add(saved.label)
            except ValueError:
                continue  # An interrupted sidecar is a missing trial.
        # Preflight may have completed only in the author tool's draft directory.
        for path in sorted((previous.parent / "drafts").glob("*/trials/*.json"), reverse=True):
            if path.name not in {"baseline.json", "oracle-0.json"} or path.resolve() != path:
                continue
            try:
                saved = TrialEvidence.model_validate_json(path.read_text())
                if saved.task_digest == digest:
                    candidates.append(saved)
            except (OSError, ValueError):
                continue
        if not candidates:
            return None
        with tempfile.TemporaryDirectory(prefix=".pending-review-", dir=root) as temporary:
            staged = Path(temporary) / "revision-0"
            retained = Path(temporary) / "prior-review"
            # Preserve all prior judge/rollout outputs outside the new catalog.
            shutil.copytree(previous, retained, symlinks=True)
            _regular_tree(retained)
            staged.mkdir()
            shutil.copytree(seed_task, staged / "task", symlinks=True)
            _regular_tree(staged)
            task = staged / "task"
            if digest_task(task) != digest:
                return None
            contract = finalize(task, source)
            if digest_task(task) != digest:
                return None
            trials = []
            discarded = []
            used_paths = set()
            reused_sources = {}
            for label, expected_reward, kwargs in _trial_plan(contract, config):
                selected = None
                for saved in candidates:
                    if saved.label != label or saved.task_digest != digest:
                        continue
                    path = Path(saved.path).resolve()
                    # Permit only this revision or this candidate's draft trials.
                    relative = path.relative_to(previous.parent)
                    parts = relative.parts
                    if not (
                        (len(parts) == 3 and parts[:2] == (previous.name, "trials"))
                        or (len(parts) == 4 and parts[0] == "drafts" and parts[2] == "trials")
                    ):
                        return None
                    if path.is_dir() and any(path.iterdir()):
                        _regular_tree(path)
                        saved.error = saved.error or inspect_execution(path)
                    else:
                        saved.error = saved.error or "Retained trial directory missing or empty"
                    model = kwargs.get("model")
                    policy_changed = model and saved.inference_digest != inference_digest(model)
                    if not saved.valid or saved.model != model or policy_changed:
                        discarded.append(
                            {
                                "label": label,
                                "path": str(path),
                                "reason": saved.error or "Model or inference policy changed",
                            }
                        )
                        continue
                    if expected_reward is not None and saved.reward != expected_reward:
                        return None  # Genuine behavior failures require an author repair.
                    if selected is None:
                        selected = saved
                if selected is None:
                    continue
                path = Path(selected.path).resolve()
                if path.name in used_paths:
                    return None
                used_paths.add(path.name)
                copied = staged / "trials" / path.name
                shutil.copytree(path, copied, symlinks=True)
                _regular_tree(copied)
                reused_sources[label] = str(path)
                selected.path = str(folder / "trials" / path.name)
                save(staged / "trials" / f"{label}.json", selected.model_dump())
                trials.append(selected)
            if not trials:
                return None
            recovery = {
                "inspection_source_sha256": hashlib.sha256(
                    Path(__file__).with_name("evaluate.py").read_bytes()
                ).hexdigest(),
                "reused_trials": [t.label for t in trials],
                "reused_trial_sources": reused_sources,
                "rerun_trials": [],
                "discarded_trials": discarded,
                "inference_digests": {
                    model: inference_digest(model)
                    for model in {*config.solver_models, config.author_model}
                },
            }
            save(
                retained / "provenance.json",
                {"resumed_from": str(previous), "task_digest": digest, "recovery": recovery},
            )
            save(
                staged / "evidence.json", _evidence_record(digest, trials, str(previous), recovery)
            )
            staged.rename(folder)
            retained.rename(archive)
        return contract, digest, trials, recovery
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.info("%s: pending review cannot be reused: %s", source["id"], exc)
        return None


async def _review_revision(
    source: dict,
    folder: Path,
    config: CampaignConfig,
    budget: Budget,
    contract: Contract,
    digest: str,
    trials: list[TrialEvidence],
    *,
    resumed_from: str | None = None,
    recovery: dict | None = None,
) -> tuple[dict, Review]:
    task = folder / "task"
    save(folder / "evidence.json", _evidence_record(digest, trials, resumed_from, recovery))
    logger.info("%s: independent specification and trajectory review", source["id"])
    result = await review(task, folder, trials, model=config.judge_model, budget=budget)
    reasons = acceptance(
        trials,
        result,
        config,
        digest,
        [m.name for m in contract.mutations],
        [e.name for e in contract.equivalents],
    )
    execution_errors = [t.model_dump() for t in trials if t.error]
    verdict = {
        "id": source["id"],
        "source": source["url"],
        "task_digest": digest,
        "status": "accepted"
        if not reasons
        else ("execution_failure" if execution_errors else "rejected"),
        "execution_errors": execution_errors,
        "score": result.score,
        "reasons": reasons,
        "task_path": str(task),
        "human_review": "pending",
        "admission_version": ADMISSION_VERSION,
        "review_path": str(folder / "review.json"),
    }
    if resumed_from is not None:
        verdict["resumed_from"] = resumed_from
    if recovery is not None:
        verdict["recovery"] = recovery
    save(folder.parent / "verdict.json", verdict)
    return verdict, result


async def _resume_validation(
    source: dict,
    root: Path,
    config: CampaignConfig,
    budget: Budget,
    seed_task: Path,
    pending: tuple[Contract, str, list[TrialEvidence], dict],
) -> dict | None:
    contract, digest, trials, recovery = pending
    folder = root.resolve() / "revision-0"
    task = folder / "task"
    resumed_from = str(seed_task.parent.resolve())
    existing = {t.label: t for t in trials}
    logger.info("%s: recovering validation with %s retained trials", source["id"], len(trials))
    for label, expected_reward, kwargs in _trial_plan(contract, config):
        if label in existing:
            continue
        recovery["rerun_trials"].append(label)
        save(folder / "evidence.json", _evidence_record(digest, trials, resumed_from, recovery))
        result = await trial(task, folder / "trials", label, config=config, budget=budget, **kwargs)
        trials.append(result)
        save(folder / "evidence.json", _evidence_record(digest, trials, resumed_from, recovery))
        if not result.valid:
            reasons = execution_gate_reasons(
                trials,
                config,
                digest,
                [m.name for m in contract.mutations],
                [e.name for e in contract.equivalents],
            )
            verdict = {
                "id": source["id"],
                "source": source["url"],
                "task_digest": digest,
                "status": "execution_failure",
                "execution_errors": [t.model_dump() for t in trials if not t.valid],
                "reasons": reasons,
                "task_path": str(task),
                "human_review": "pending",
                "admission_version": ADMISSION_VERSION,
                "resumed_from": resumed_from,
                "recovery": recovery,
            }
            save(root / "verdict.json", verdict)
            return verdict
        if expected_reward is not None and result.reward != expected_reward:
            # Retain this failure for the author; do not retry the same behavior.
            return None
    reasons = execution_gate_reasons(
        trials,
        config,
        digest,
        [m.name for m in contract.mutations],
        [e.name for e in contract.equivalents],
    )
    if reasons:
        raise ValueError(
            "Recovered validation did not satisfy admission gates: " + "; ".join(reasons)
        )
    verdict, _ = await _review_revision(
        source,
        folder,
        config,
        budget,
        contract,
        digest,
        trials,
        resumed_from=resumed_from,
        recovery=recovery,
    )
    return verdict


async def curate_one(
    source: dict, root: Path, config: CampaignConfig, budget: Budget, seed_task: Path | None = None
) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    save(root / "source.json", source)
    first_revision = 0
    if seed_task is not None:
        pending = _prepare_pending_review(seed_task, root, source, config)
        if pending is not None:
            verdict = await _resume_validation(source, root, config, budget, seed_task, pending)
            if verdict is not None and verdict["status"] != "rejected":
                return verdict
            # A control failure or quality rejection needs an author repair.
            # Preserve the reviewed revision instead of rerolling its judge.
            seed_task = root.resolve() / "revision-0/task"
            first_revision = 1
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
            previous_evidence = sorted((seed_task.parent / "trials").glob("*.json"))
            if previous_evidence:
                feedback += "\nPrevious validation evidence:\n" + "\n".join(
                    p.read_text()[:12000] for p in previous_evidence[:3]
                )
            previous_review = seed_task.parent / "review.json"
            if previous_review.is_file():
                feedback += (
                    "\nPrevious independent review to address:\n"
                    + (previous_review.read_text()[:24000])
                )
        for revision in range(first_revision, first_revision + config.max_revisions):
            last_verdict = None
            execution_errors = []
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
                runtime=config.author_runtime,
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
                execution_errors = [t.model_dump() for t in trials if t.error]
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
            trials.append(
                await trial(
                    task,
                    folder / "trials",
                    "pytest-tamper",
                    config=config,
                    budget=budget,
                    script=pytest_tamper(contract.source_paths),
                )
            )
            for equivalent in contract.equivalents:
                trials.append(
                    await trial(
                        task,
                        folder / "trials",
                        f"equivalent-{equivalent.name}",
                        config=config,
                        budget=budget,
                        script=equivalent.script,
                        mutation=True,
                    )
                )
            bad = [
                t
                for t in trials
                if not t.valid
                or t.reward != (1 if t.label.startswith(("oracle", "equivalent-")) else 0)
            ]
            if bad:
                execution_errors = [t.model_dump() for t in bad if t.error]
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
            verdict, result = await _review_revision(
                source, folder, config, budget, contract, digest, trials
            )
            execution_errors = verdict["execution_errors"]
            last_verdict = verdict
            if not verdict["reasons"]:
                return verdict
            feedback = (
                "Repair this task using the independent review, then revalidate.\n"
                + result.model_dump_json()
                + "\nGate failures: "
                + json.dumps(verdict["reasons"])
            )
        return {
            **(last_verdict or {}),
            "id": source["id"],
            "source": source["url"],
            "status": "execution_failure" if execution_errors else "rejected",
            "execution_errors": execution_errors,
            "reasons": ["Revision limit reached", feedback],
            "human_review": "pending",
        }
    finally:
        try:
            await sandbox.stop()
        finally:
            budget.settle(reserve, 0.15 + (time.monotonic() - started) * 0.00005, estimated=True)


@contextmanager
def campaign_lock(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"A campaign is already running in {out}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def campaign(
    seeds: list[str], out: Path, config: CampaignConfig, *, retry_rejected: bool = False
) -> dict:
    with campaign_lock(out.resolve()):
        return await _campaign(seeds, out, config, retry_rejected=retry_rejected)


async def _campaign(
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
    current_admissions = []
    for admitted in manifest["accepted"]:
        task = out / "tasks" / admitted["id"]
        if digest_task(task) != admitted["task_digest"]:
            raise ValueError(f"Released task changed: {admitted['id']}")
        if admitted.get("admission_version") == ADMISSION_VERSION:
            current_admissions.append(admitted)
        else:
            destination = out / "superseded" / f"{admitted['id']}-{time.time_ns()}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(task, destination)
            manifest.setdefault("previous_attempts", []).append(
                {**admitted, "status": "needs_revalidation", "archived_task": str(destination)}
            )
    manifest["accepted"] = current_admissions
    manifest["status"] = "running"
    manifest["seeds"] = list(dict.fromkeys([*manifest.get("seeds", []), *seeds]))
    manifest["in_progress"] = []
    h = hashlib.sha256()
    for p in sorted(Path(__file__).parent.glob("*.py")):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    manifest["runtime"] = {
        "harness_digest": h.hexdigest(),
        "versions": {
            name: version(name)
            for name in ("repo2rlenv", "harbor", "modal", "langgraph", "litellm")
        },
    }
    save(manifest_path, manifest)
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
        manifest["in_progress"].append(url)
        save(manifest_path, manifest)
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
        manifest["in_progress"].remove(url)
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
