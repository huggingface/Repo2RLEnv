"""Retain Harbor tasks with inspectable labels and immutable evidence bindings.

No model calls, target imports, subprocesses, or provider operations occur here.
Historical tasks are copied; the original directory and trial bytes stay intact.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import tomllib
from pathlib import Path

import tomli_w

from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.emitter.evaluation import (
    EvaluationLabel,
    EvidenceReference,
    Provenance,
    evaluation_time,
)
from repo2rlenv.quality.loop.models import LoopResult, TrialRecord


def _digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Evidence must be a real file: {path}")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _identity(task: Path) -> str:
    record = inspect_bundle(task)
    if record["claimed_hash"] is not None and not record["integrity_passed"]:
        raise ValueError("Task differs from its recorded bundle hash")
    return record["bundle_hash"]


def read_evaluation(task: Path) -> EvaluationLabel:
    """Read an advisory label; unlabeled historical tasks remain unverified."""
    identity = _identity(task)
    configuration = tomllib.loads((task / "task.toml").read_text())
    value = configuration.get("metadata", {}).get("repo2env", {}).get("evaluation")
    if value is None:
        return EvaluationLabel(subject_bundle_hash=identity)
    label = EvaluationLabel.model_validate(value)
    if label.subject_bundle_hash not in {None, identity}:
        raise ValueError("Evaluation label belongs to a different executable bundle")
    return label


def _trial_evidence(
    trial: TrialRecord, parent_hash: str, original_task: Path | None = None
) -> EvidenceReference:
    path = Path(trial.result)
    if _digest(path) != trial.result_sha256:
        raise ValueError(f"Trial result changed after collection: {path}")
    raw = json.loads(path.read_text())
    agent = raw.get("config", {}).get("agent", {})
    if (
        (raw.get("verifier_result") or {}).get("rewards", {}).get("reward") != trial.reward
        or (raw.get("exception_info") or {}).get("exception_type") != trial.exception_type
        or (agent.get("name") or agent.get("import_path") or "unknown") != trial.agent
        or agent.get("model_name") != trial.model
    ):
        raise ValueError(f"Trial summary disagrees with its raw evidence: {path}")
    expected = {"baseline": "nop", "oracle": "oracle", "probe": "oracle"}.get(trial.role)
    if (expected and trial.agent != expected) or (
        trial.role == "rollout" and trial.agent in {"nop", "oracle", "unknown"}
    ):
        raise ValueError("Trial used the wrong agent for its evidence role")
    exit_path = path.parent / "agent/exit-code.txt"
    exit_code = int(exit_path.read_text()) if exit_path.exists() else None
    if trial.agent_exit_code != exit_code:
        raise ValueError("Trial exit-code summary disagrees with its saved log")
    if trial.role != "probe":
        if trial.bundle_hash != parent_hash:
            raise ValueError("Control or rollout evidence belongs to another bundle")
    else:
        # Probes change the private solution. Their own hash must remain distinct
        # and their preserved creation receipt must identify the reviewed parent.
        probe_task = Path(raw.get("config", {}).get("task", {}).get("path", ""))
        if not probe_task.is_dir():
            # Worker trials record the sandbox path. Their controller keeps the
            # same probe under quality/probes/KEY/TASK, beside trials/KEY.
            probe_task = path.parents[4] / "probes" / path.parents[2].name / probe_task.name
        manifest_path = probe_task.parent / "probe.json"
        _digest(manifest_path)
        manifest = json.loads(manifest_path.read_text())
        if (
            trial.probe is None
            or manifest.get("parent_hash") != parent_hash
            or manifest.get("bundle_hash") != trial.bundle_hash
            or manifest.get("probe") != trial.probe.model_dump()
            or _identity(probe_task) != trial.bundle_hash
        ):
            raise ValueError("Semantic probe is not bound to this parent task")
        log = path.parent / "agent/oracle.txt"
        completed = log.is_file() and "__QUALITY_PROBE_COMPLETED__" in log.read_text()
        if not completed or not trial.probe_installed:
            raise ValueError("Semantic probe did not preserve its execution completion marker")
    # Native and worker execution both preserve a controller receipt two levels
    # above the raw Harbor trial. Imported checksum-only evidence is supported
    # against its original physical directory, never the newly labeled copy.
    if trial.binding == "receipt":
        receipt_path = path.parents[2] / "trial.json"
        _digest(receipt_path)
        receipt = json.loads(receipt_path.read_text())
        if (
            receipt.get("state") != "completed"
            or receipt.get("bundle_hash") != trial.bundle_hash
            or receipt.get("result_sha256") != trial.result_sha256
        ):
            raise ValueError("Trial controller receipt does not bind the recorded evidence")
    else:
        from repo2rlenv.quality.loop.artifacts import parse_task

        source = Path(raw.get("config", {}).get("task", {}).get("path", ""))
        if not source.is_dir() and trial.role != "probe" and original_task is not None:
            # Imported CPU results contain sandbox paths. The exact local task
            # snapshot is an equally strong binding only when its physical
            # Harbor checksum also matches; matching our bundle hash alone is
            # insufficient because evaluation overlays change that checksum.
            source = original_task
        if _identity(source) != trial.bundle_hash or parse_task(source).checksum != raw.get(
            "task_checksum"
        ):
            raise ValueError("Original Harbor checksum evidence is unavailable or changed")
    return EvidenceReference(
        kind=trial.role,
        path=str(path.resolve()),
        sha256=trial.result_sha256,
        subject_bundle_hash=trial.bundle_hash,
        harbor_task_checksum=raw.get("task_checksum"),
    )


def _verified_result(result: LoopResult, task: Path) -> None:
    from repo2rlenv.quality.loop.runner import (
        control_failures,
        probe_failures,
        required_probe_focus,
    )

    failures = control_failures(result.trials, 1.0) + probe_failures(result.trials, 1.0)
    probes = [item.probe for item in result.trials if item.role == "probe" and item.probe]
    rollouts = [item for item in result.trials if item.role == "rollout"]
    if (
        failures
        or result.reasons
        or result.review is None
        or not result.review.sound
        or result.review.rollout not in {"legitimate_success", "legitimate_failure"}
        or {probe.kind for probe in probes} != {"wrong_solution", "valid_alternative"}
        or required_probe_focus(task)
        - {probe.focus for probe in probes if probe.kind == "wrong_solution"}
        or not rollouts
        or rollouts[-1].exception_type not in {None, "AgentTimeoutError"}
    ):
        raise ValueError("Usable result lacks the required reviewed controls and blind rollout")


def label_from_quality(
    task: Path, result_path: Path, *, provenance: Provenance = "unknown"
) -> EvaluationLabel:
    """Translate a saved quality result, independently checking acceptance proof.

    Nonaccepted attempts survive missing trial files and retain that diagnosis.
    A claimed usable result with missing/tampered evidence cannot become verified.
    """
    identity = _identity(task)
    result_hash = _digest(result_path)
    result = LoopResult.model_validate_json(result_path.read_text())
    if result.bundle_hash != identity:
        raise ValueError("Quality result belongs to a different executable bundle")
    status, stage, code = {
        "usable": ("verified", "complete", "quality_verified"),
        "reviewed": ("unverified", "review", "validation_incomplete"),
        "needs_evidence": ("unverified", "review", "evidence_missing"),
        "needs_repair": ("needs_repair", "repair", "quality_defect"),
        "budget_exhausted": ("blocked", "review", "budget_exhausted"),
    }[result.status]
    references = [
        EvidenceReference(
            kind="quality_result",
            path=str(result_path.resolve()),
            sha256=result_hash,
            subject_bundle_hash=identity,
        )
    ]
    reasons = list(result.reasons)
    codes = [code]
    if status == "verified":
        _verified_result(result, task)
    for trial in result.trials:
        try:
            original = Path(result.task_path)
            references.append(
                _trial_evidence(trial, identity, original if original.is_dir() else task)
            )
        except (ValueError, OSError, KeyError, IndexError) as exc:
            if status == "verified":
                raise ValueError(f"Cannot verify task: {exc}") from exc
            if "evidence_unavailable" not in codes:
                codes.append("evidence_unavailable")
            reasons.append(str(exc))
    return EvaluationLabel(
        status=status,
        stage=stage,
        reason_codes=codes,
        detail="; ".join(reasons)
        or "Reviewed controls, semantic probes and blind rollout passed the quality profile.",
        checked_at=evaluation_time(),
        provenance=provenance,
        subject_bundle_hash=identity,
        profile=result.profile,
        evidence=references,
    )


def write_labeled_copy(task: Path, destination: Path, label: EvaluationLabel) -> Path:
    """Atomically publish a new labeled directory, never editing a historical task."""
    task, destination = task.absolute(), destination.absolute()
    if destination == task or destination.is_relative_to(task):
        raise ValueError("Labeled output must be outside the source task")
    identity = _identity(task)
    if label.subject_bundle_hash not in {None, identity}:
        raise ValueError("Evaluation label belongs to a different executable bundle")
    if label.status == "verified":
        results = [item for item in label.evidence if item.kind == "quality_result"]
        if len(results) != 1 or _digest(Path(results[0].path)) != results[0].sha256:
            raise ValueError("Verified label must retain its unchanged quality result")
        checked = label_from_quality(task, Path(results[0].path), provenance=label.provenance)
        if checked.status != "verified" or checked.evidence != label.evidence:
            raise ValueError("Verified label disagrees with checked quality evidence")
    original_toml = _digest(task / "task.toml")
    label = EvaluationLabel.model_validate(
        {
            **label.model_dump(),
            "subject_bundle_hash": identity,
            "source_task_path": str(task),
            "source_task_toml_sha256": original_toml,
        }
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = destination.parent / f".{destination.name}.evaluation.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(fd)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        with tempfile.TemporaryDirectory(prefix=".evaluation-", dir=destination.parent) as temp:
            copied = Path(temp) / task.name
            # Copy links as links so inspect_bundle rejects a source race instead
            # of following an introduced link outside the task directory.
            shutil.copytree(task, copied, symlinks=True)
            if _identity(copied) != identity or _digest(copied / "task.toml") != original_toml:
                raise ValueError("Source changed during label export")
            path = copied / "task.toml"
            configuration = tomllib.loads(path.read_text())
            metadata = configuration.setdefault("metadata", {}).setdefault("repo2env", {})
            metadata["evaluation"] = label.toml_metadata()
            path.write_text(tomli_w.dumps(configuration))
            if _identity(copied) != identity:
                raise ValueError("Evaluation annotations changed the executable bundle identity")
            if _identity(task) != identity or _digest(task / "task.toml") != original_toml:
                raise ValueError("Source changed during label export")
            copied.rename(destination)
        return destination
    finally:
        lock.unlink()
