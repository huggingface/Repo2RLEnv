"""Construction evidence is useful without claiming full quality acceptance."""

import hashlib
import json
from pathlib import Path

from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.emitter.evaluation import EvaluationLabel, EvidenceReference, evaluation_time
from repo2rlenv.quality.labels import read_evaluation, write_labeled_copy


def retain(task: Path, destination: Path, report: Path) -> Path:
    identity = inspect_bundle(task)
    if not identity["integrity_passed"]:
        raise ValueError("Task changed after construction")
    quality = json.loads(report.read_text())
    if quality.get("bundle_hash") != identity["bundle_hash"] or not quality.get(
        "construction_verified"
    ):
        raise ValueError("Construction report does not describe this task")
    result = Path(quality["reference_repeat_result"])
    raw = json.loads(result.read_text())
    observed = (raw.get("verifier_result") or {}).get("rewards", {}).get("reward")
    if raw.get("exception_info") or observed != quality["reference_reward"]:
        raise ValueError("Repeated reference does not support the construction report")
    # The execution receipt binds the exact reference bundle, not its parent seed.
    receipts = [p / "trial.json" for p in result.parents if (p / "trial.json").is_file()]
    if not receipts:
        raise ValueError("Reference execution receipt is missing")
    receipt = json.loads(receipts[0].read_text())
    if (
        receipt.get("bundle_hash") != identity["bundle_hash"]
        or receipt.get("result_sha256") != hashlib.sha256(result.read_bytes()).hexdigest()
    ):
        raise ValueError("Reference execution receipt does not bind this bundle and result")
    evidence = [
        EvidenceReference(
            kind=kind,
            path=str(path.resolve()),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            subject_bundle_hash=identity["bundle_hash"],
        )
        for kind, path in (("diagnosis", report), ("oracle", result))
    ]
    label = EvaluationLabel(
        status="unverified",
        stage="construction",
        reason_codes=["optimization_construction_passed", "full_quality_not_run"],
        detail="Baseline, sampled solutions, continuous-score diversity and reference repeatability checked. Full adversarial and rollout review is not established.",
        checked_at=evaluation_time(),
        provenance="unattended",
        profile="frontiersmith-v1",
        subject_bundle_hash=identity["bundle_hash"],
        evidence=evidence,
    )
    if destination.exists():
        existing = read_evaluation(destination)
        if (
            not inspect_bundle(destination)["integrity_passed"]
            or existing.subject_bundle_hash != label.subject_bundle_hash
            or existing.evidence != label.evidence
            or existing.reason_codes != label.reason_codes
        ):
            raise ValueError("Existing annotated task differs from its construction evidence")
        return destination
    return write_labeled_copy(task, destination, label)
