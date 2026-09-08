"""Bind an emitted package to five explicit episode/verification contracts."""

from __future__ import annotations

from repo2rlenv.tasksmith.authoring import Construction, Design
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.models import (
    ArtifactRef,
    CollectionRule,
    EpisodeContract,
    ExposureRule,
    MaterializationContract,
    OracleContract,
    PRIdentity,
    Requirement,
    SourcePins,
    TaskBundle,
    TaskContract,
    VerifierContract,
    digest,
)


def bind_contracts(
    config: TasksmithConfig, source: dict, construction: dict, patch: ArtifactRef
) -> TaskBundle:
    value = Construction.model_validate(construction["artifact"])
    design = Design.model_validate(construction["design"])
    receipt = construction["source_receipt"]
    if patch.sha256 != receipt["patch_digest"]:
        raise ValueError("Captured reference patch differs from frozen source evidence")
    for key in ("repo", "base_sha", "head_sha", "base_tree_sha", "head_tree_sha"):
        if receipt["source"].get(key) != source[key]:
            raise ValueError("Source receipt differs from frozen PR identity")
    collection = [CollectionRule(source=p, destination=p) for p in value.contract.source_paths]
    materialization = MaterializationContract(
        collection=collection,
        mode="source",
        clean_destination="/workspace",
        origin_checks=[
            value.source_origin_probe,
            "Exact trusted solver-to-host-to-grader file inventory equality",
        ],
        commands=[
            "Replace declared source roots in the clean grader; reuse base editable mapping offline"
        ],
    )
    return TaskBundle(
        task=TaskContract(
            pr=PRIdentity.from_url(source["url"]),
            revision=construction.get("task_revision", 0),
            parent_digest=construction.get("parent_task_digest"),
            source=SourcePins(
                before_commit=source["base_sha"],
                reference_commit=source["head_sha"],
                head_commit=source["head_sha"],
                before_tree=source["base_tree_sha"],
                reference_tree=source["head_tree_sha"],
                merge_base=source["base_sha"],
                patch_digest=receipt["patch_digest"],
                captured_at=receipt["captured_at"],
            ),
            useful_outcome=design.selected.task_request,
            requirements=[
                Requirement(
                    id=r.id, statement=r.behavior, public_evidence=value.public_evidence[r.id]
                )
                for r in value.contract.requirements
            ],
            exclusions=design.selected.exclusions,
            editable_paths=value.contract.source_paths,
            collection=collection,
            exposure=[
                ExposureRule(path="instruction.md", visibility="solver"),
                ExposureRule(path="environment", visibility="solver"),
                ExposureRule(path="tests", visibility="grader"),
                ExposureRule(path="solution", visibility="oracle"),
            ],
        ),
        episode=EpisodeContract(
            initial_source=source["base_sha"],
            reset="Fresh immutable base image and fresh private grader for every trial",
            observations=[
                "Natural task instruction",
                "Sanitized base repository and offline dependencies",
            ],
            tools=["Bounded shell in the unprivileged remote solver workspace"],
            cpus=2,
            memory_mib=8192,
            seed_policy="Fixed fixture seeds and hash seed zero; no external downloads",
            horizon_seconds=config.trial_timeout_sec,
            termination="Agent completion or fixed deadline; incomplete infrastructure is not a reward",
            disclosed_transformations=[
                "Git history and caches removed; source changes collected only from declared roots"
            ],
        ),
        verifier=VerifierContract(
            requirement_checks={r.id: r.tests for r in value.contract.requirements},
            expected_values=value.expected_values,
            cases=value.cases,
            protected_runner="Separate clean root-owned pytest interpreter; submitted observations run as uid1000",
            tolerance_policy="Explicit test-local numerical tolerances grounded in independent expectations",
            control_ids=["baseline", "oracle-0", "oracle-1", "tamper", "challenge"]
            + [f"negative-{i}" for i in range(len(value.contract.mutations))]
            + [f"positive-{i}" for i in range(len(value.contract.equivalents))],
        ),
        oracle=OracleContract(
            source_commit=source["head_sha"],
            patch=patch,
            preconditions=["Frozen base source", value.reference_explanation],
            requirement_ids=[r.id for r in value.contract.requirements],
            adaptations=value.reference_adaptations,
            materialization_digest=digest(materialization),
        ),
        materialization=materialization,
    )
