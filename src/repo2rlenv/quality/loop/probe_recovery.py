"""Evidence-bound correction of controls that never completed installation."""

from __future__ import annotations

import json
from pathlib import Path

from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.loop.artifacts import digest, import_trial, task_identity
from repo2rlenv.quality.loop.models import SemanticProbe, TrialRecord


def _files(trial: TrialRecord) -> dict[str, str | None]:
    result = Path(trial.result)
    paths = [
        result,
        result.parents[2] / "trial.json",
        result.parent / "agent/oracle.txt",
        result.parent / "agent/exit-code.txt",
    ]
    hashes = {}
    for path in paths:
        if any(component.is_symlink() for component in (path, *path.parents)) or (
            path.exists() and not path.is_file()
        ):
            raise ValueError("Probe installation evidence must be regular files")
        hashes[str(path)] = digest(path) if path.exists() else None
    return hashes


def capture_attempt(parent_hash: str, trial: TrialRecord) -> dict:
    from repo2rlenv.quality.loop.probe_behavior import BEHAVIOR_FOCI, behavior_files

    attempt = {
        "parent_hash": parent_hash,
        "trial": trial.model_dump(mode="json"),
        "files": _files(trial),
    }
    if trial.probe is not None and trial.probe.focus in BEHAVIOR_FOCI:
        attempt["behavior_files"] = behavior_files(trial)
    return attempt


def record_attempt(directory: Path, key: str, parent_hash: str, trial: TrialRecord) -> dict:
    """Keep prior installations even after current-revision trials are discarded."""
    attempt = capture_attempt(parent_hash, trial)
    path = directory / "probe-attempts" / f"{key}.json"
    if path.exists():
        if path.is_symlink():
            raise ValueError("Preserved probe installation evidence changed")
        saved = json.loads(path.read_text())
        # Older journals retain their original evidence boundary. Do not seal
        # previously unbound verifier logs retroactively on resume.
        compared = (
            attempt
            if "behavior_files" in saved
            else {key: value for key, value in attempt.items() if key != "behavior_files"}
        )
        if saved != compared:
            raise ValueError("Preserved probe installation evidence changed")
        return saved
    else:
        save_record(path, attempt)
    return attempt


def installation(attempt: dict) -> bool:
    """Reconstruct completion from the owned receipt, exact variant and saved logs.

    An absent receipt/log or uncertain exit is not proof of a failed installation.
    These are content bindings, not attestations for externally supplied evidence.
    """
    trial = TrialRecord.model_validate(attempt["trial"])
    if trial.role != "probe" or trial.probe is None or trial.binding != "receipt":
        raise ValueError("Probe correction requires an owned probe execution receipt")
    if attempt["files"] != _files(trial) or any(
        value is None for value in attempt["files"].values()
    ):
        raise ValueError("Probe installation evidence is missing or changed")
    result = Path(trial.result)
    if digest(result) != trial.result_sha256:
        raise ValueError("Probe result changed after collection")
    raw = json.loads(result.read_text())
    variant = Path(raw.get("config", {}).get("task", {}).get("path", ""))
    if not variant.is_dir():
        variant = result.parents[4] / "probes" / result.parents[2].name / variant.name
    manifest_path = variant.parent / "probe.json"
    if (
        any(component.is_symlink() for component in (manifest_path, *manifest_path.parents))
        or not manifest_path.is_file()
    ):
        raise ValueError("Probe creation receipt is unavailable")
    manifest = json.loads(manifest_path.read_text())
    if (
        manifest
        != {
            "parent_hash": attempt["parent_hash"],
            "bundle_hash": trial.bundle_hash,
            "probe": trial.probe.model_dump(mode="json"),
        }
        or task_identity(variant) != trial.bundle_hash
    ):
        raise ValueError("Probe installation belongs to another definition or parent")
    imported = import_trial(result.parents[2] / "trial.json", variant, "oracle")
    expected = trial.model_copy(update={"role": "oracle", "probe": None, "probe_installed": None})
    if imported != expected:
        raise ValueError("Probe summary disagrees with its execution evidence")
    log = result.parent / "agent/oracle.txt"
    if log.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Probe installation log exceeds the evidence limit")
    completed = "__QUALITY_PROBE_COMPLETED__" in log.read_text()
    if completed != trial.probe_installed or trial.agent_exit_code is None:
        raise ValueError("Probe installation summary disagrees with its completion evidence")
    if not completed and trial.agent_exit_code == 0:
        raise ValueError("Missing marker without a failed agent exit is inconclusive")
    return completed


def failed_installations(task: Path, trials: list[TrialRecord]) -> list[dict]:
    """Only expose independently reconstructed failures to the model."""
    failures = []
    parent_hash = task_identity(task)
    for index, trial in enumerate(trials):
        if (
            trial.role != "probe"
            or trial.probe is None
            or trial.probe.kind != "wrong_solution"
            or trial.probe_installed is not False
            or trial.agent_exit_code in {None, 0}
        ):
            continue
        try:
            attempt = capture_attempt(parent_hash, trial)
            if installation(attempt):
                continue
        except (ValueError, OSError, KeyError, IndexError):
            continue
        failures.append(
            {
                "name": trial.probe.name,
                "result": trial.result,
                "result_sha256": trial.result_sha256,
                "agent_exit_code": trial.agent_exit_code,
                "summary_path": f"evidence/{index}-probe/result.json",
                "log_path": f"evidence/{index}-probe/agent/oracle.txt",
                "diagnosis": "Probe installation did not complete; reward is not a semantic control result.",
            }
        )
    return failures


def grounded_diagnosis(failure: dict, review, context) -> bool:
    """Require a probe-category issue citing this attempt, not an unrelated defect."""
    for issue in review.issues:
        if issue.category != "probe":
            continue
        for citation in issue.evidence:
            if citation.path not in {
                failure["summary_path"],
                failure["log_path"],
                *failure.get("evidence_paths", []),
            }:
                continue
            document = context.documents.get(citation.path, "")
            if document and " ".join(citation.quote.split()) in " ".join(document.split()):
                return True
    return False


def replacement_evidence(
    task: Path, probe: SemanticProbe, history: list[dict], failures: list[dict], review, context
) -> dict | None:
    """A completed installation under this name permanently closes this exception."""
    if probe.kind != "wrong_solution" or not any(
        failure["name"] == probe.name for failure in failures
    ):
        return None
    attempts = [
        attempt
        for attempt in history
        if (attempt["trial"].get("probe") or {}).get("name") == probe.name
    ]
    if not attempts:
        return None
    try:
        if any(installation(attempt) for attempt in attempts):
            return None
    except (ValueError, OSError, KeyError, IndexError):
        return None
    exact = [
        attempt
        for attempt in attempts
        if attempt["parent_hash"] == task_identity(task)
        and attempt["trial"]["probe"] == probe.model_dump(mode="json")
    ]
    for failure in failures:
        if (
            failure["name"] == probe.name
            and any(attempt["trial"]["result"] == failure["result"] for attempt in exact)
            and grounded_diagnosis(failure, review, context)
        ):
            return {"probe": probe.model_dump(mode="json"), "attempts": attempts}
    return None
