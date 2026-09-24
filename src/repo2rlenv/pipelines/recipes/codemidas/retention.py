"""Advisory labels for the paper profile, separate from the shared quality loop."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.emitter.evaluation import EvaluationLabel, EvidenceReference, evaluation_time
from repo2rlenv.quality.labels import read_evaluation, write_labeled_copy


def retain(task: Path, destination: Path, controls: Path, *, audit: Path | None = None) -> Path:
    """Copy an immutable task with checked control and optional audit references.

    The standard `verified` status belongs to the shared semantic-probe profile.
    This paper reproduction reports its own completed stages without claiming it.
    """
    from repo2rlenv.pipelines.recipes.codemidas.audit import control_evidence, curriculum

    identity = inspect_bundle(task)["bundle_hash"]
    evidence = [
        EvidenceReference(
            kind="baseline" if item["agent"] == "nop" else "oracle",
            path=item["result"],
            sha256=item["sha256"],
            subject_bundle_hash=identity,
        )
        for item in control_evidence(controls, identity)
    ]
    status, stage = "unverified", "controls"
    codes = ["codemidas_controls_passed", "rollout_review_not_run"]
    detail = "CodeMidas: two baseline failures and four oracle passes; rollout audit pending."
    if audit is not None:
        result = json.loads(audit.read_text())
        if result["bundle_hash"] != identity:
            raise ValueError("Audit belongs to a different executable bundle")
        classification = curriculum(result["screen_rewards"])
        if classification != result["curriculum"]:
            raise ValueError("Audit curriculum summary disagrees with its recorded outcomes")
        evidence.append(
            EvidenceReference(
                kind="diagnosis",
                path=str(audit.resolve()),
                sha256=hashlib.sha256(audit.read_bytes()).hexdigest(),
                subject_bundle_hash=identity,
            )
        )
        stage = (
            ("rollout" if classification == "incomplete" else "complete")
            if result["method_sound"]
            else "review"
        )
        if result["method_sound"]:
            codes = [
                "codemidas_method_sound",
                "curriculum_" + classification,
                "standard_profile_not_run",
            ]
            detail = (
                "CodeMidas controls and independent rollout audit passed; curriculum="
                + classification
                + ". The separate shared quality-loop profile has not run."
            )
        else:
            if result.get("adversarial_status") == "blocked" and result.get("solver_review_sound"):
                status, stage, codes = (
                    "blocked",
                    "probes",
                    ["provider_policy_blocked", "codemidas_solver_review_passed"],
                )
                detail = (
                    "Controls and four solver reviews passed; the provider blocked the "
                    "adversarial stage. Full CodeMidas soundness has not been established."
                )
            else:
                status, codes = "needs_repair", ["codemidas_audit_defect"]
                detail = (
                    "CodeMidas audit identified a material defect; inspect the linked evidence."
                )
    label = EvaluationLabel(
        status=status,
        stage=stage,
        reason_codes=codes,
        detail=detail,
        checked_at=evaluation_time(),
        provenance="unattended",
        subject_bundle_hash=identity,
        profile="codemidas-v1",
        evidence=evidence,
    )
    if destination.exists():
        existing = read_evaluation(destination)
        if (
            existing.subject_bundle_hash != identity
            or existing.evidence != label.evidence
            or existing.reason_codes != label.reason_codes
            or existing.status != label.status
        ):
            raise ValueError("Retained task or its evidence changed; use a new destination")
        return destination
    return write_labeled_copy(task, destination, label)
