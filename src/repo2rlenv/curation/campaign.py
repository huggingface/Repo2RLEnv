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
from repo2rlenv.curation.artifacts import (
    digest_task,
    finalize,
    release_task,
    sanitize_generated_python_caches,
)
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.cloud import AuthorSandbox
from repo2rlenv.curation.design import plan_candidate_design
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
    quality_gate_reasons,
    review_scores,
    validate_review_scores,
)
from repo2rlenv.curation.prompts import AUTHOR
from repo2rlenv.curation.protocol import (
    CONVERSION_AUTHOR,
    PILOT_AUTHOR,
    DraftLimitExceeded,
    DraftTracker,
    MechanicalLimitExceeded,
    MechanicalTracker,
    check_verification_plan,
)
from repo2rlenv.curation.repair import RepairSession, SeedRepair, prepare_seed_repair
from repo2rlenv.curation.review import review, validate_review_receipt
from repo2rlenv.curation.review_evidence import ReviewEvidenceError
from repo2rlenv.curation.sources import resolve_pr
from repo2rlenv.curation.specification_review import SpecificationInputError, review_specification
from repo2rlenv.curation.specification_review import _snapshot as specification_snapshot
from repo2rlenv.curation.verifier_review import VerifierInputError, review_verifier
from repo2rlenv.curation.verifier_review import _snapshot as verifier_snapshot

logger = logging.getLogger(__name__)
ADMISSION_VERSION = 6


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


class ValidationExecutionError(RuntimeError):
    """Incomplete execution is not an automatic request to rewrite a task."""


def _require_completed_trials(trials: list[TrialEvidence]) -> None:
    invalid = [trial for trial in trials if not trial.valid]
    if invalid:
        raise ValidationExecutionError(
            "Validation did not complete; author repair was not started: "
            + json.dumps([trial.model_dump() for trial in invalid])
        )


class _AuthorPhases:
    """One-run author sessions sharing time, counters and the original ledger."""

    def __init__(self, root, source, config, budget, first_revision):
        self.path = root / "author-phase.json"
        self.source, self.config, self.budget = source, config, budget
        self.sandbox, self.active, self.started = None, None, None
        self.state = {
            "status": "ready",
            "source": source,
            "config_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
            "active_seconds": 0.0,
            "used_author_revisions": first_revision,
            "sessions": [],
            "budget_scope": budget.scope,
            "budget_group": budget.group,
        }
        # Same-candidate restart is deliberately not a fresh author allowance.
        # A separate evidence-recovery path must reconcile a retained journal.
        try:
            with self.path.open("x") as stream:
                json.dump(self.state, stream, indent=2)
        except FileExistsError as exc:
            raise RecoveryError("Existing author phase requires explicit recovery") from exc

    async def start(self):
        if self.active is not None and not self.closed:
            raise RecoveryError("Previous author session is not confirmed closed")
        remaining = int(self.config.author_timeout_sec - self.state["active_seconds"])
        if remaining <= 0:
            raise TimeoutError("Cumulative author active-time allowance exhausted")
        cloud_remaining = self.config.author_cloud_allowance_usd - sum(
            session["estimated_usd"] for session in self.state["sessions"]
        )
        cloud_seconds = int((cloud_remaining - 0.15) / 0.00005)
        if cloud_seconds <= 0:
            raise BudgetExceeded("Cumulative author cloud allowance exhausted")
        remaining = min(remaining, cloud_seconds)
        self.sandbox = AuthorSandbox(remaining)
        reservation = self.budget.reserve(cloud_remaining, "cloud:author:" + self.source["id"])
        self.started = time.monotonic()
        self.active = {"reservation": reservation, "timeout_sec": remaining, "status": "starting"}
        self.state["sessions"].append(self.active)
        self.state["status"] = "authoring"
        save(self.path, self.state)
        await self.sandbox.start()
        self.active.update(status="running", sandbox_id=self.sandbox.sandbox.object_id)
        save(self.path, self.state)
        await self.sandbox.prepare(self.source)
        return self.sandbox

    @property
    def closed(self):
        return self.active is not None and self.active["status"] == "closed"

    def start_revision(self, revision):
        if revision != self.state["used_author_revisions"]:
            raise RecoveryError("Author phase revision counter mismatch")
        self.state["used_author_revisions"] = revision + 1
        save(self.path, self.state)

    async def close(self):
        if self.active is None or self.active["status"] not in {"starting", "running"}:
            return
        self.active["status"] = self.state["status"] = "stopping"
        save(self.path, self.state)
        try:
            await self.sandbox.stop()
        except BaseException as exc:
            self.active["status"] = self.state["status"] = "stop_uncertain"
            self.active["error"] = f"{type(exc).__name__}: {exc}"
            save(self.path, self.state)
            # Do not free a reservation for a potentially live sandbox, and do
            # not retry stop/settlement implicitly from the outer finally.
            raise
        elapsed = max(0.0, time.monotonic() - self.started)
        self.state["active_seconds"] += elapsed
        self.active.update(status="stopped_unsettled", active_seconds=elapsed)
        self.state["status"] = "stopped_unsettled"
        save(self.path, self.state)
        estimate = 0.15 + elapsed * 0.00005
        self.budget.settle(self.active["reservation"], estimate, estimated=True)
        self.active.update(status="closed", estimated_usd=estimate)
        self.state["status"] = "author_closed"
        save(self.path, self.state)

    async def handoff(self, task, digest, revision, trials):
        if digest_task(task) != digest or any(attempt.task_digest != digest for attempt in trials):
            raise RecoveryError("Author handoff task or preflight digest changed")
        folder = task.parent
        (folder / "trials").mkdir(exist_ok=True)
        for attempt in trials:
            path = Path(attempt.path)
            if not path.is_relative_to(folder):
                _regular_tree(path)
                copied = folder / "trials" / path.name
                copied.parent.mkdir(exist_ok=True)
                shutil.copytree(path, copied, dirs_exist_ok=True)
                attempt.path = str(copied)
            save(folder / "trials" / f"{attempt.label}.json", attempt.model_dump())
        save(folder / "evidence.json", _evidence_record(digest, trials, None))
        self.state["handoff"] = {"task": str(task), "task_digest": digest, "revision": revision}
        save(self.path, self.state)
        await self.close()
        if digest_task(task) != digest:
            raise RecoveryError("Frozen task changed while closing author sandbox")
        self.state["status"] = "validating"
        save(self.path, self.state)


def _regular_tree(path: Path) -> None:
    """Reject links and special files before copying retained sandbox evidence."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Not a regular evidence directory: {path}")
    for entry in path.rglob("*"):
        mode = entry.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError(f"Non-regular evidence entry: {entry}")


def latest_checkpoint(parent: Path) -> Path | None:
    """Select the latest task in the newest usable timestamp-named attempt.

    Resumed tasks preserve file mtimes, so those timestamps must never order
    separate attempts. Only direct revision and validation-draft checkpoints
    count; retained review archives and nested task copies are not candidates.
    """
    if parent.is_symlink() or not parent.is_dir():
        return None
    attempts = sorted(
        (
            path
            for path in parent.iterdir()
            if path.name.isascii()
            and path.name.isdecimal()
            and not path.is_symlink()
            and path.is_dir()
        ),
        key=lambda path: (int(path.name), path.name),
        reverse=True,
    )
    for attempt in attempts:
        folders = [
            (path, 1)
            for path in attempt.glob("revision-*")
            if path.name.removeprefix("revision-").isascii()
            and path.name.removeprefix("revision-").isdecimal()
        ]
        drafts = attempt / "drafts"
        if not drafts.is_symlink() and drafts.is_dir():
            folders.extend((path, 0) for path in drafts.iterdir())
        checkpoints = []
        for folder, is_revision in folders:
            task = folder / "task"
            if folder.is_symlink() or not folder.is_dir():
                continue
            try:
                if not (task / "contract.json").is_file():
                    continue
                _regular_tree(task)
                # An author may change implementation files without changing
                # the contract. Avoid checkpoint-directory mtimes, which also
                # change when later trial or review evidence is persisted.
                modified = max(
                    path.stat().st_mtime_ns for path in task.rglob("*") if path.is_file()
                )
            except (OSError, ValueError):
                continue
            number = folder.name.removeprefix("revision-") if is_revision else folder.name
            ordinal = int(number) if number.isascii() and number.isdecimal() else -1
            # A tied revision retains evaluation evidence for the same export;
            # numeric ordinals make revision-10/drafts/10 sort after 2.
            checkpoints.append((modified, is_revision, ordinal, folder.name, task))
        if checkpoints:
            return max(checkpoints)[-1]
    return None


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
    # The independent review must distinguish an exploit from a mislabeled
    # legitimate solution. Reward alone cannot establish audit compliance.
    plan.append(("adversary", None, {"model": config.author_model, "adversary": True}))
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
                prior_result = Review.model_validate_json(prior_review.read_text())
            except ValueError:
                pass  # An interrupted or malformed response has no valid verdict.
            else:
                metadata = previous / "evidence.json"
                current = (
                    metadata.exists()
                    and json.loads(metadata.read_text()).get("admission_version")
                    == ADMISSION_VERSION
                )
                if (
                    not current
                    or prior_result.adversary_assessment == "attempted_hack"
                    or quality_gate_reasons(prior_result, config)
                ):
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
            if evidence.get("admission_version") not in (None, 3, 4, 5, ADMISSION_VERSION):
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
                    policy_changed = model and saved.inference_digest != inference_digest(
                        model, adversary=kwargs.get("adversary", False)
                    )
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
                "audit_inference_digest": inference_digest(config.author_model, adversary=True),
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
    result = await review(
        task,
        folder,
        trials,
        model=config.judge_model,
        budget=budget,
        acceptance_policy=config.acceptance_policy,
    )
    result = validate_review_receipt(
        folder,
        task,
        trials,
        model=config.judge_model,
        acceptance_policy=config.acceptance_policy,
    )
    if result.adversary_assessment != "attempted_hack":
        for attempt in trials:
            if attempt.label == "adversary":
                attempt.error = "Incomplete adversarial audit: " + result.adversary_assessment
                save(folder / "trials/adversary.json", attempt.model_dump())
        save(folder / "evidence.json", _evidence_record(digest, trials, resumed_from, recovery))
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
        **review_scores(result, config),
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
    if reasons and not all(reason.startswith("Adversarial trial") for reason in reasons):
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
    source: dict,
    root: Path,
    config: CampaignConfig,
    budget: Budget,
    seed_task: Path | None = None,
    *,
    seed_repair: SeedRepair | None = None,
) -> dict:
    phase_journal = root / "author-phase.json"
    if phase_journal.exists() or phase_journal.is_symlink():
        raise RecoveryError("Existing author phase requires explicit recovery")
    if seed_repair is None:
        return await _curate_one(source, root, config, budget, seed_task)
    if seed_task is None:
        raise RecoveryError("Explicit seed repair requires its retained task")
    with prepare_seed_repair(seed_repair, seed_task, root, source, config, budget) as repair:
        seed_task = repair.restore_task(seed_task)
        try:
            result = await _curate_one(
                source, root, config, repair.budget, seed_task, repair=repair
            )
        finally:
            save(root / "repair-accounting.json", repair.accounting())
        result.update(
            kind=seed_repair.kind,
            parent_task_digest=seed_repair.parent_task_digest,
            repair_input_path=str(root / "repair-input.json"),
        )
        save(root / "repair-result.json", result)
        return result


async def _curate_one(
    source: dict,
    root: Path,
    config: CampaignConfig,
    budget: Budget,
    seed_task: Path | None = None,
    *,
    repair: RepairSession | None = None,
) -> dict:
    phase_journal = root / "author-phase.json"
    if phase_journal.exists() or phase_journal.is_symlink():
        raise RecoveryError("Existing author phase requires explicit recovery")
    root.mkdir(parents=True, exist_ok=True)
    save(root / "source.json", source)
    drafts = DraftTracker(root / "submitted-drafts.json", config.max_candidate_drafts)
    initial_design = repair.design if repair else None
    conversion = config.submission_policy == "conversion"
    mechanics = MechanicalTracker(
        root / "mechanical-submissions.json", config.max_mechanical_submissions
    )
    if conversion and seed_task is not None and repair is None:
        raise RecoveryError(
            "Conversion policy requires a fresh candidate; do not reset legacy repair allowances"
        )

    def structural_feedback(message: str) -> str:
        return message if conversion else repair_feedback(message)

    def repair_feedback(message: str) -> str:
        if drafts.limit is not None and len(drafts.rows) >= drafts.limit:
            save(root / "repair-limit-feedback.json", {"feedback": message, "drafts": drafts.rows})
            raise DraftLimitExceeded(
                f"Submitted draft limit {drafts.limit} exhausted; final failure retained in repair-limit-feedback.json"
            )
        return message

    def submit(task: Path) -> Contract:
        try:
            if conversion:
                sanitation = sanitize_generated_python_caches(task)
                save(task.parent / "sanitation.json", sanitation)
                plan_path = task / "verification-plan.json"
                if initial_design is not None and not plan_path.exists():
                    save(plan_path, initial_design.verification_plan.model_dump())
                    save(
                        task.parent / "metadata-repairs.json",
                        {"restored": ["verification-plan.json"], "from": str(root / "design.json")},
                    )
            if initial_design is not None:
                save(
                    task / "authoring-context.json",
                    {
                        "source": {key: source.get(key) for key in ("url", "base_sha", "head_sha")},
                        "screening_observations": source.get("screening_observations", []),
                        "initial_design": initial_design.model_dump(),
                    },
                )
            contract = finalize(task, source)
            if not conversion:
                drafts.observe(digest_task(task), task)
            if config.require_verification_plan:
                check_verification_plan(task, contract)
            if conversion:
                # Input completeness is a cheap host check before semantic allowance
                # or paid review. Never hide arbitrary helpers/binary fixtures.
                specification_snapshot(task)
                verifier_snapshot(task)
        except (ValueError, SpecificationInputError, VerifierInputError) as exc:
            if conversion:
                mechanics.fail(task, str(exc))
            else:
                drafts.observe(digest_task(task), task)
            raise ValueError(str(exc)) from exc
        if conversion:
            if repair:
                repair.require_unreviewed_change(digest_task(task))
            drafts.observe(digest_task(task), task)
        return contract

    async def check_specification(task: Path) -> str | None:
        # Timestamped attempts of this candidate share specification findings.
        # Keep prior judge scores outside every final-review revision tree.
        for enabled, reviewer, kind in (
            (config.specification_review, review_specification, "specification"),
            (config.verifier_review, review_verifier, "verifier"),
        ):
            if not enabled:
                continue
            try:
                result = await reviewer(task, root.parent, model=config.judge_model, budget=budget)
            except (SpecificationInputError, VerifierInputError) as exc:
                return (
                    f"Repair the {kind} review inputs before remote validation: {exc}. "
                    "Keep the instruction and complete verifier/reference source as readable "
                    "text within the stated limits. Remove generated caches such as "
                    "__pycache__ from tests and solution; package runtime data under environment. "
                    "Do not remove required behavior or hide source from review. "
                    "Then call validate_candidate again."
                )
            if not result.passed:
                return (
                    f"Repair this independent {kind} preflight before remote validation. "
                    "Address the cited contract, fixture or assertion defects while keeping "
                    "the task substantive. Then call validate_candidate again.\n"
                    + result.model_dump_json()
                )
        return None

    first_revision = repair.used if repair else 0
    seed_specification_feedback = None
    if seed_task is not None and repair is None:
        pending = _prepare_pending_review(seed_task, root, source, config)
        if pending is None:
            # A preflight rejection has no execution trials to recover. Still
            # restore its cached findings before another author/model round.
            seed_specification_feedback = await check_specification(seed_task)
        else:
            seed_specification_feedback = await check_specification(root / "revision-0/task")
            verdict = (
                None
                if seed_specification_feedback
                else await _resume_validation(source, root, config, budget, seed_task, pending)
            )
            if verdict is not None and verdict["status"] != "rejected":
                return verdict
            # A control failure or quality rejection needs an author repair.
            # Preserve the reviewed revision instead of rerolling its judge.
            seed_task = root.resolve() / "revision-0/task"
            first_revision = 1
    phases = (
        _AuthorPhases(root, source, config, budget, first_revision)
        if config.release_author_before_validation
        else None
    )
    sandbox = None if phases else AuthorSandbox(config.author_timeout_sec)
    reserve = (
        None
        if phases
        else budget.reserve(config.author_cloud_allowance_usd, "cloud:author:" + source["id"])
    )
    started = time.monotonic()
    last_task = seed_task
    attempt = (
        max((int(p.name) for p in (root / "drafts").glob("*") if p.name.isdecimal()), default=0)
        if repair
        else 0
    )
    cached = {}

    async def export_submission(task: Path) -> None:
        try:
            await sandbox.export(task)
        except ValueError as exc:
            # A symlink, size violation or invalid export cannot be hashed safely.
            # Count each such submission rather than allowing unlimited tool retries.
            if conversion:
                mechanics.fail(task, "Candidate export failed: " + str(exc))
            else:
                drafts.observe(f"unexportable-{len(drafts.rows) + 1}", task)
                repair_feedback("Candidate export failed: " + str(exc))
            raise

    async def defer_candidate(reason: str) -> str:
        save(root / "deferred.json", {"reason": reason, "source": source["url"]})
        raise CandidateDeferred(reason)

    async def validate_candidate() -> str:
        nonlocal attempt
        attempt += 1
        folder = root / "drafts" / str(attempt)
        task = folder / "task"
        try:
            await export_submission(task)
            submit(task)
        except ValueError as exc:
            return structural_feedback("Structural validation failed: " + str(exc))
        specification_feedback = await check_specification(task)
        if specification_feedback:
            return repair_feedback(specification_feedback)
        digest = digest_task(task)
        if digest not in cached or any(t.error for t in cached[digest]):
            logger.info("%s: remote baseline and oracle, draft %s", source["id"], attempt)
            cached[digest] = await preflight(task, folder / "trials", config=config, budget=budget)
        if phases:
            _require_completed_trials(cached[digest])
        if (
            len(cached[digest]) != 2
            or {t.label for t in cached[digest]} != {"baseline", "oracle-0"}
            or any(
                not t.valid or t.reward != (0 if t.label == "baseline" else 1)
                for t in cached[digest]
            )
        ):
            repair_feedback(evidence_summary(cached[digest]))
        return evidence_summary(cached[digest])

    try:
        if phases:
            sandbox = await phases.start()
        else:
            await sandbox.start()
        save(
            root / "sandbox.json",
            {"id": sandbox.sandbox.object_id, "timeout_sec": config.author_timeout_sec},
        )
        if not phases:
            await sandbox.prepare(source)
        feedback = "Investigate this PR and create the task.\n" + json.dumps(source)
        if conversion:
            feedback += f"\nSemantic submission limit: {config.max_candidate_drafts}; mechanical failed-input limit: {config.max_mechanical_submissions}. All work shares the original cost and turn limits."
        if config.require_verification_plan and seed_task is None:
            design = await plan_candidate_design(
                source=source,
                root=root,
                shell=sandbox.shell,
                budget=budget,
                model=config.author_model,
                runtime=config.author_runtime,
            )
            initial_design = design
            prepared = json.loads(await sandbox.shell("mkdir -p /output/task"))
            if prepared["exit_code"]:
                raise RuntimeError("Could not prepare the planned task directory")
            await sandbox.write(
                "/output/task/verification-plan.json",
                design.verification_plan.model_dump_json(indent=2),
            )
            feedback += (
                "\nThe planning phase accepted and saved this design. Its verification plan "
                "is already in /output/task/verification-plan.json. Implement it and check "
                "the real fixtures before submitting a complete task. Schema acceptance "
                "is not a semantic review or proof that the reference works.\n"
                + design.model_dump_json()
            )
        if seed_task is not None:
            digest_task(seed_task)
            for p in seed_task.rglob("*"):
                if p.is_file():
                    await sandbox.sandbox.filesystem.copy_from_local.aio(
                        p, "/output/task/" + p.relative_to(seed_task).as_posix()
                    )
            if repair:
                feedback += (
                    "\nThe retained task is restored in /output/task for autonomous repair. "
                    "Update its contract/verification-plan consistently when correcting supported defects. "
                    "Call validate_candidate on the repaired task. Remaining allowances: "
                    f"{drafts.limit - len(drafts.rows)} semantic submissions, "
                    f"{mechanics.limit - len(mechanics.rows)} mechanical failures, "
                    f"{config.max_revisions - repair.used} author revisions." + repair.feedback
                )
            else:
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
            if seed_specification_feedback:
                feedback += "\n" + seed_specification_feedback
        end_revision = config.max_revisions if repair else first_revision + config.max_revisions
        for revision in range(first_revision, end_revision):
            last_verdict = None
            execution_errors = []
            logger.info("%s: author revision %s", source["id"], revision + 1)
            if phases and phases.closed:
                # Only an actual task/control/review failure reaches a next
                # revision. Infrastructure errors escape without another author.
                _regular_tree(last_task)
                if digest_task(last_task) != phases.state["handoff"]["task_digest"]:
                    raise RecoveryError("Frozen task changed before author repair")
                sandbox = await phases.start()
                for path in last_task.rglob("*"):
                    if path.is_file():
                        await sandbox.sandbox.filesystem.copy_from_local.aio(
                            path, "/output/task/" + path.relative_to(last_task).as_posix()
                        )
                feedback += (
                    "\nThis is a fresh author sandbox with the pinned repository and the last "
                    "exported task restored in /output/task. Prior exploratory packages and "
                    "temporary repository edits are absent. Existing submission, revision, "
                    "active-time and budget allowances remain shared."
                )
            if repair:
                repair.start_revision(revision)
            if phases:
                phases.start_revision(revision)
            await run_agent(
                model=config.author_model,
                system=AUTHOR
                + (
                    CONVERSION_AUTHOR
                    if conversion
                    else PILOT_AUTHOR
                    if config.require_verification_plan
                    else ""
                ),
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
            try:
                await export_submission(task)
                contract = submit(task)
            except ValueError as exc:
                feedback = structural_feedback(
                    "Repair the structural validation failure: " + str(exc)
                )
                continue
            last_task = task
            specification_feedback = await check_specification(task)
            if specification_feedback:
                feedback = repair_feedback(specification_feedback)
                continue
            digest = digest_task(task)
            trials = list(
                cached.get(digest)
                or await preflight(task, folder / "trials", config=config, budget=budget)
            )
            if phases:
                _require_completed_trials(trials)
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
                repair_feedback(feedback)
                continue
            if phases:
                await phases.handoff(task, digest, revision, trials)

            async def full_trial(
                label, task=task, folder=folder, trials=trials, digest=digest, **kwargs
            ):
                if phases and digest_task(task) != digest:
                    raise RecoveryError("Frozen task changed before validation")
                result = await trial(
                    task, folder / "trials", label, config=config, budget=budget, **kwargs
                )
                trials.append(result)
                if phases:
                    save(folder / "evidence.json", _evidence_record(digest, trials, None))
                    if result.task_digest != digest or digest_task(task) != digest:
                        raise RecoveryError("Frozen task or trial digest changed during validation")
                    _require_completed_trials([result])

            # Cheap correctness gates precede paid model rollouts.
            for i in range(1, config.oracle_repeats):
                await full_trial(f"oracle-{i}")
            await full_trial("tamper", script=TAMPER)
            for mutation in contract.mutations:
                await full_trial(f"mutation-{mutation.name}", script=mutation.script, mutation=True)
            await full_trial("pytest-tamper", script=pytest_tamper(contract.source_paths))
            for equivalent in contract.equivalents:
                await full_trial(
                    f"equivalent-{equivalent.name}", script=equivalent.script, mutation=True
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
                repair_feedback(feedback)
                continue
            for model_index, model in enumerate(config.solver_models):
                for k in range(config.solver_attempts):
                    logger.info("%s: solver %s attempt %s", source["id"], model, k + 1)
                    await full_trial(f"solver-{model_index}-{k}", model=model)
            await full_trial("adversary", model=config.author_model, adversary=True)
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
            if result.adversary_assessment != "attempted_hack":
                return verdict
            if not verdict["reasons"]:
                return verdict
            feedback = (
                "Repair this task using the independent review, then revalidate.\n"
                + result.model_dump_json()
                + "\nGate failures: "
                + json.dumps(verdict["reasons"])
            )
            repair_feedback(feedback)
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
            if conversion:
                save(
                    root / "construction-accounting.json",
                    {
                        "submission_policy": config.submission_policy,
                        "semantic_submissions": len(drafts.rows),
                        "mechanical_failures": len(mechanics.rows),
                        "source_unsuitability_established": False,
                        "note": "Construction outcomes do not establish source unsuitability. Author deferrals require independent review.",
                    },
                )
        finally:
            if phases:
                await phases.close()
            else:
                try:
                    await sandbox.stop()
                finally:
                    budget.settle(
                        reserve, 0.15 + (time.monotonic() - started) * 0.00005, estimated=True
                    )


class RecoveryError(ValueError):
    """Retained evidence conflicts; stop before authoring or admission."""


def _candidate_budget(
    root: Path, manifest: dict, url: str, budget: Budget, config: CampaignConfig
) -> Budget:
    """Persist one scope before work; recover a single legacy timestamped scope."""
    scopes = manifest.setdefault("budget_scopes", {})
    known = {scopes[url]} if url in scopes else set()
    known.update(
        row["budget_scope"]
        for row in manifest["accepted"]
        + manifest["rejected"]
        + manifest.get("previous_attempts", [])
        if row.get("source") == url and row.get("budget_scope")
    )
    stable = f"candidate:{url}"
    legacy_prefix = url + ":"
    if budget.path.exists():
        entries = json.loads(budget.path.read_text())["entries"].values()
        known.update(
            entry["scope"]
            for entry in entries
            if entry.get("scope") == stable
            or (
                isinstance(entry.get("scope"), str)
                and entry["scope"].startswith(legacy_prefix)
                and entry["scope"][len(legacy_prefix) :].isascii()
                and entry["scope"][len(legacy_prefix) :].isdecimal()
            )
        )
    if len(known) > 1:
        raise RecoveryError(f"Multiple candidate budget scopes require reconciliation: {url}")
    scope = next(iter(known), stable)
    if not isinstance(scope, str) or not (
        scope == stable
        or (
            scope.startswith(legacy_prefix)
            and scope[len(legacy_prefix) :].isascii()
            and scope[len(legacy_prefix) :].isdecimal()
        )
    ):
        raise RecoveryError(f"Invalid candidate budget scope: {url}")
    scopes[url] = scope
    save(root / "manifest.json", manifest)
    return Budget(budget.path, budget.limit, scope=scope, scope_limit=config.max_candidate_usd)


def _recovery_regular(path: Path, *, directory: bool = False) -> None:
    if path.resolve() != path or not path.exists():
        raise RecoveryError(f"Missing or linked recovery evidence: {path}")
    mode = path.stat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise RecoveryError(f"Non-regular recovery evidence: {path}")


def _accepted_identity(row: dict) -> str:
    identity = row.get("id")
    if (
        not isinstance(identity, str)
        or identity in {"", ".", ".."}
        or Path(identity).name != identity
    ):
        raise RecoveryError("Invalid accepted task identity")
    return identity


def _validate_accepted(root: Path, row: dict, config: CampaignConfig) -> Path:
    """Recompute all admission gates from the exact retained revision, without calls."""
    _recovery_regular(root / "config.json")
    if CampaignConfig.model_validate_json((root / "config.json").read_text()) != config:
        raise RecoveryError("Accepted recovery configuration mismatch")
    identity = _accepted_identity(row)
    if row.get("status") != "accepted" or row.get("admission_version") != ADMISSION_VERSION:
        raise RecoveryError("Accepted recovery needs current admission version")
    task = Path(row["task_path"])
    try:
        relative = task.relative_to(root / "candidates" / identity)
    except ValueError as exc:
        raise RecoveryError("Accepted task is outside its candidate") from exc
    if (
        len(relative.parts) != 3
        or not relative.parts[0].isascii()
        or not relative.parts[0].isdecimal()
        or relative.parts[2] != "task"
        or not relative.parts[1].startswith("revision-")
        or not relative.parts[1].removeprefix("revision-").isascii()
        or not relative.parts[1].removeprefix("revision-").isdecimal()
    ):
        raise RecoveryError("Accepted task is not a retained revision")
    _recovery_regular(task, directory=True)
    for entry in task.rglob("*"):
        _recovery_regular(entry, directory=entry.is_dir())
    digest = digest_task(task)
    if digest != row.get("task_digest"):
        raise RecoveryError("Accepted task digest mismatch")
    folder = task.parent
    source_path = folder.parent / "source.json"
    evidence_path, review_path = folder / "evidence.json", folder / "review.json"
    verdict_path = folder.parent / "verdict.json"
    for path in (source_path, evidence_path, review_path, verdict_path, task / "contract.json"):
        _recovery_regular(path)
    durable = json.loads(verdict_path.read_text())
    if any(
        row.get(key) != durable.get(key)
        for key in (
            "id",
            "source",
            "status",
            "task_digest",
            "task_path",
            "review_path",
            "score",
            "admission_version",
            "acceptance_policy",
            "validity_score",
            "legacy_score",
            "intrinsic_difficulty_score",
        )
    ):
        raise RecoveryError("Accepted result and durable verdict disagree")
    source = json.loads(source_path.read_text())
    if source.get("id") != identity or source.get("url") != row.get("source"):
        raise RecoveryError("Accepted source identity mismatch")
    if row.get("review_path") != str(review_path):
        raise RecoveryError("Accepted review path mismatch")
    evidence = json.loads(evidence_path.read_text())
    if (
        evidence.get("task_digest") != digest
        or evidence.get("admission_version") != ADMISSION_VERSION
    ):
        raise RecoveryError("Accepted execution evidence digest or admission mismatch")
    trials = [TrialEvidence.model_validate(t) for t in evidence["trials"]]
    if any(trial.task_digest != digest for trial in trials):
        raise RecoveryError("Accepted trial evidence digest mismatch")
    try:
        result = validate_review_receipt(
            review_path.parent,
            task,
            trials,
            model=config.judge_model,
            acceptance_policy=config.acceptance_policy,
        )
    except (ReviewEvidenceError, OSError) as exc:
        raise RecoveryError(f"Accepted review evidence is invalid: {exc}") from exc
    contract = Contract.model_validate_json((task / "contract.json").read_text())
    reasons = acceptance(
        trials,
        result,
        config,
        digest,
        [m.name for m in contract.mutations],
        [e.name for e in contract.equivalents],
    )
    try:
        validate_review_scores(row, result, config)
    except ValueError as exc:
        raise RecoveryError(str(exc)) from exc
    if row.get("reasons") != [] or row.get("execution_errors", []):
        raise RecoveryError("Accepted verdict score or outcome mismatch")
    if reasons:
        raise RecoveryError("Accepted evidence fails admission: " + "; ".join(reasons))
    return task


def _reconcile_accepted(root: Path, manifest: dict, config: CampaignConfig, budget: Budget) -> None:
    """Recover verdict/release -> manifest interruptions before scheduling any work."""
    retained = {}
    recorded_old = {
        (row.get("source"), row.get("id"), row.get("task_digest"), row.get("admission_version"))
        for row in manifest["accepted"] + manifest.get("previous_attempts", [])
        if row.get("admission_version") != ADMISSION_VERSION
        and row.get("status") in {"accepted", "needs_revalidation"}
    }
    for path in sorted((root / "candidates").glob("*/*/verdict.json")):
        _recovery_regular(path)
        row = json.loads(path.read_text())
        if row.get("status") != "accepted":
            continue
        if row.get("admission_version") != ADMISSION_VERSION:
            key = (
                row.get("source"),
                row.get("id"),
                row.get("task_digest"),
                row.get("admission_version"),
            )
            if key not in recorded_old:
                raise RecoveryError("Orphan accepted verdict needs explicit admission revalidation")
            continue
        task = _validate_accepted(root, row, config)
        if task.parent.parent / "verdict.json" != path:
            raise RecoveryError("Accepted verdict references a different attempt")
        url = row["source"]
        if url not in manifest["seeds"] or url in retained:
            raise RecoveryError(f"Unknown or duplicate accepted source: {url}")
        retained[url] = (row, task, path)
    if any(row.get("status") != "accepted" for row in manifest["accepted"]):
        raise RecoveryError("Accepted manifest contains a non-accepted result")
    for row in manifest["accepted"]:
        _accepted_identity(row)
        if row.get("source") not in manifest["seeds"]:
            raise RecoveryError("Accepted manifest source is outside recorded seeds")
    completed = {}
    for row in manifest["accepted"] + manifest["rejected"]:
        url = row["source"]
        if url in completed:
            raise RecoveryError(f"Duplicate completed source: {url}")
        completed[url] = row
        if row["status"] == "accepted" and row.get("admission_version") == ADMISSION_VERSION:
            _validate_accepted(root, row, config)
            if url not in retained:
                raise RecoveryError(f"Accepted manifest has no durable verdict: {url}")
    for url, (row, _task, _) in retained.items():
        existing = completed.get(url)
        if existing is not None and any(
            existing.get(key) != row.get(key)
            for key in (
                "status",
                "id",
                "task_digest",
                "task_path",
                "review_path",
                "score",
                "admission_version",
            )
        ):
            raise RecoveryError(f"Manifest and durable accepted verdict disagree: {url}")
        destination = root / "tasks" / row["id"]
        if destination.exists() or destination.is_symlink():
            _recovery_regular(destination, directory=True)
            _regular_tree(destination)
            if digest_task(destination) != row["task_digest"]:
                raise RecoveryError(f"Released accepted task digest mismatch: {url}")
    accepted_ids = {row["id"] for row, _, _ in retained.values()}
    accepted_ids.update(
        row["id"]
        for row in manifest["accepted"]
        if row.get("admission_version") != ADMISSION_VERSION
    )
    for path in (root / "tasks").glob("*"):
        if path.name.startswith(".release-"):
            continue  # An interrupted atomic copy was never a released task.
        if path.name not in accepted_ids:
            raise RecoveryError(f"Released task has no accepted durable evidence: {path}")
    # Validate the whole retained set first; failures cannot trigger regeneration.
    for url, (row, task, path) in retained.items():
        scoped = _candidate_budget(root, manifest, url, budget, config)
        release_task(task, root / "tasks" / row["id"])
        if url not in completed:
            row = {**row, "recovered_verdict": str(path)}
            manifest["accepted"].append(row)
        else:
            row = completed[url]
        row.update(budget_scope=scoped.scope, charged_or_reserved_usd=scoped.spent)
        manifest["charged_or_reserved_usd"] = budget.spent
        save(root / "manifest.json", manifest)


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
    retained = any((out / name).exists() for name in ("manifest.json", "candidates", "tasks"))
    if retained and (not config_path.is_file() or not (out / "budget.json").is_file()):
        raise RecoveryError(
            "Retained campaign requires its original configuration and budget ledger"
        )
    if config_path.exists():
        _recovery_regular(config_path)
    if (
        config_path.exists()
        and CampaignConfig.model_validate_json(config_path.read_text()) != config
    ):
        raise ValueError(
            "Resume requires the original config; use a new output directory for a changed campaign"
        )
    save(config_path, config.model_dump())
    budget = Budget(out / "budget.json", config.budget_usd)
    if budget.path.exists():
        _recovery_regular(budget.path)
    initial_spend = budget.spent  # Create the empty ledger before the first durable manifest.
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        _recovery_regular(manifest_path)
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
    manifest.setdefault("charged_or_reserved_usd", initial_spend)
    manifest["seeds"] = list(dict.fromkeys([*manifest.get("seeds", []), *seeds]))
    # Resolve all known scopes before migration or workers can create new charges.
    # This also rejects ambiguous historical spend for completed/retried sources.
    for url in manifest["seeds"]:
        _candidate_budget(out, manifest, url, budget, config)
    _reconcile_accepted(out, manifest, config, budget)
    if retry_rejected:
        manifest.setdefault("previous_attempts", []).extend(manifest["rejected"])
        manifest["rejected"] = []
    current_admissions = []
    for admitted in manifest["accepted"]:
        task = out / "tasks" / admitted["id"]
        _recovery_regular(task, directory=True)
        _regular_tree(task)
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
        scoped = _candidate_budget(out, manifest, url, budget, config)
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
            seed_task = latest_checkpoint(parent)
            candidate_dir = parent / str(time.time_ns())
            result = await curate_one(source, candidate_dir, config, scoped, seed_task)
            if result["status"] == "accepted":
                if result.get("id") != source["id"] or result.get("source") != url:
                    raise RecoveryError("Accepted verdict does not match the current source")
                task = _validate_accepted(out, result, config)
                release_task(task, out / "tasks" / source["id"])
        except CandidateDeferred as exc:
            result = {"source": url, "status": "rejected", "reasons": ["Deferred: " + str(exc)]}
        except MechanicalLimitExceeded as exc:
            result = {
                "source": url,
                "status": "construction_failure",
                "failure_stage": "mechanical_inputs",
                "reasons": [str(exc)],
            }
        except DraftLimitExceeded as exc:
            result = {
                "source": url,
                "status": "construction_failure",
                "failure_stage": "semantic_revision_limit",
                "reasons": [str(exc)],
            }
            if config.submission_policy == "legacy":
                result = {
                    "source": url,
                    "status": "rejected",
                    "reasons": [type(exc).__name__ + ": " + str(exc)],
                }
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
        if config.submission_policy == "conversion":
            result["source_unsuitability_established"] = False
        result.update(budget_scope=scoped.scope, charged_or_reserved_usd=scoped.spent)
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
