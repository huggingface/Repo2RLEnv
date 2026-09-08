"""Small, provider-neutral Tasksmith contracts; these models never execute task code."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitSHA = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Nonempty = Annotated[str, Field(min_length=1)]


def canonical_json(value: BaseModel | dict | list) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: BaseModel | dict | list) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def safe_relative(path: str) -> str:
    value = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or "\0" in path
        or value.is_absolute()
        or ".." in value.parts
        or not value.parts
        or str(value) != path
    ):
        raise ValueError(f"Expected a canonical relative path: {path!r}")
    return path


def _nonoverlapping(paths: list[str]) -> None:
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            left, right = PurePosixPath(path), PurePosixPath(other)
            if left == right or left in right.parents or right in left.parents:
                raise ValueError(f"Overlapping paths: {path!r}, {other!r}")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


class PRIdentity(StrictModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    number: int = Field(gt=0, strict=True)

    @field_validator("repository")
    @classmethod
    def canonical_repository(cls, value: str) -> str:
        if any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("Invalid repository identity")
        return value.lower()

    @property
    def key(self) -> str:
        return f"github.com/{self.repository}/pull/{self.number}"

    @property
    def url(self) -> str:
        return f"https://{self.key}"

    @classmethod
    def from_url(cls, url: str) -> PRIdentity:
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or parsed.query
            or parsed.fragment
            or len(parts) != 4
            or parts[2] != "pull"
            or not parts[3].isdigit()
        ):
            raise ValueError("Expected an exact HTTPS GitHub pull-request URL")
        return cls(repository="/".join(parts[:2]), number=int(parts[3]))


class SourcePins(StrictModel):
    before_commit: GitSHA
    reference_commit: GitSHA
    patch_digest: Digest
    captured_at: float = Field(gt=0)
    head_commit: GitSHA | None = None
    before_tree: GitSHA | None = None
    reference_tree: GitSHA | None = None
    merge_base: GitSHA | None = None


class ArtifactRef(StrictModel):
    sha256: Digest
    size_bytes: int = Field(ge=0, strict=True)
    path: str
    media_type: str = "application/octet-stream"

    _safe_path = field_validator("path")(safe_relative)


class LedgerRef(StrictModel):
    """Identity only. Reservations and charges remain exclusively in curation.budget.Budget."""

    path: Nonempty
    scope: Nonempty
    group: Nonempty | None = None


class Deadline(StrictModel):
    expires_at: float = Field(gt=0)

    def remaining(self, *, now: float | None = None) -> float:
        return max(0.0, self.expires_at - (time.time() if now is None else now))

    def require_remaining(self, *, now: float | None = None) -> float:
        remaining = self.remaining(now=now)
        if remaining <= 0:
            raise TimeoutError("The original Tasksmith deadline has expired")
        return remaining


class Requirement(StrictModel):
    id: Identifier
    statement: Nonempty
    public_evidence: list[Nonempty] = Field(min_length=1)


class CollectionRule(StrictModel):
    source: str
    destination: str
    required: Literal[True] = True

    _safe_paths = field_validator("source", "destination")(safe_relative)


class ExposureRule(StrictModel):
    path: str
    visibility: Literal["solver", "grader", "oracle", "audit"]

    _safe_path = field_validator("path")(safe_relative)


class TaskContract(StrictModel):
    pr: PRIdentity
    source: SourcePins
    revision: int = Field(ge=0, strict=True)
    parent_digest: Digest | None = None
    useful_outcome: Nonempty
    requirements: list[Requirement] = Field(min_length=1)
    exclusions: list[Nonempty] = Field(default_factory=list)
    editable_paths: list[str] = Field(min_length=1)
    collection: list[CollectionRule] = Field(min_length=1)
    exposure: list[ExposureRule] = Field(min_length=1)

    @field_validator("editable_paths")
    @classmethod
    def safe_edits(cls, value: list[str]) -> list[str]:
        for path in value:
            safe_relative(path)
        _nonoverlapping(value)
        return value

    @model_validator(mode="after")
    def coherent_boundary(self) -> TaskContract:
        if (self.revision == 0) != (self.parent_digest is None):
            raise ValueError("Only revision zero has no parent digest")
        if len({row.id for row in self.requirements}) != len(self.requirements):
            raise ValueError("Requirement IDs must be unique")
        _nonoverlapping([row.destination for row in self.collection])
        _nonoverlapping([row.source for row in self.collection])
        for path in self.editable_paths:
            if not any(
                path == row.source or PurePosixPath(row.source) in PurePosixPath(path).parents
                for row in self.collection
            ):
                raise ValueError(f"Editable path is not completely collected: {path}")
        for index, row in enumerate(self.exposure):
            for other in self.exposure[index + 1 :]:
                if row.visibility != other.visibility:
                    _nonoverlapping([row.path, other.path])
        return self


class EpisodeContract(StrictModel):
    initial_source: GitSHA
    assets: list[ArtifactRef] = Field(default_factory=list)
    disclosed_transformations: list[Nonempty] = Field(default_factory=list)
    reset: Nonempty
    observations: list[Nonempty] = Field(min_length=1)
    tools: list[Nonempty] = Field(min_length=1)
    network: Literal["none"] = "none"
    cpus: float = Field(gt=0)
    memory_mib: int = Field(gt=0, strict=True)
    seed_policy: Nonempty
    horizon_seconds: int = Field(gt=0, strict=True)
    termination: Nonempty
    reward_policy: Literal["deterministic_binary"] = "deterministic_binary"
    infrastructure_policy: Literal["incomplete"] = "incomplete"


class ExpectedValue(StrictModel):
    provenance: Literal[
        "public_math", "fixed_fixture", "trusted_primitive", "independent_reference"
    ]
    explanation: Nonempty
    evidence_ids: list[Nonempty] = Field(min_length=1)


class VerifierContract(StrictModel):
    requirement_checks: dict[str, list[Nonempty]] = Field(min_length=1)
    expected_values: dict[str, ExpectedValue] = Field(min_length=1)
    cases: dict[str, list[Nonempty]] = Field(min_length=1)
    protected_runner: Nonempty
    tolerance_policy: Nonempty
    control_ids: list[Identifier] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_check_map(self) -> VerifierContract:
        if any(not rows for rows in self.requirement_checks.values()):
            raise ValueError("Every requirement needs an observable check")
        checks = {check for rows in self.requirement_checks.values() for check in rows}
        if set(self.expected_values) != checks or set(self.cases) != checks:
            raise ValueError(
                "Every mapped check needs cases and independent expected-value provenance"
            )
        if any(not rows for rows in self.cases.values()):
            raise ValueError("Empty cases cannot establish behavioral coverage")
        return self


class MaterializationContract(StrictModel):
    collection: list[CollectionRule] = Field(min_length=1)
    mode: Literal["source", "install", "compile", "service"]
    clean_destination: Nonempty
    offline: Literal[True] = True
    commands: list[Nonempty] = Field(default_factory=list)
    derived_outputs: list[str] = Field(default_factory=list)
    origin_checks: list[Nonempty] = Field(min_length=1)
    source_change_evidence: list[Nonempty] = Field(default_factory=list)

    @model_validator(mode="after")
    def bounded_destination(self) -> MaterializationContract:
        destination = PurePosixPath(self.clean_destination)
        if (
            not destination.is_absolute()
            or destination == PurePosixPath("/")
            or ".." in destination.parts
            or str(destination) != self.clean_destination
        ):
            raise ValueError("Materialization needs a canonical, non-root absolute destination")
        _nonoverlapping([row.destination for row in self.collection])
        for output in self.derived_outputs:
            safe_relative(output)
        if self.mode != "source" and not self.commands:
            raise ValueError(
                "Install/compile/service materialization requires explicit offline commands"
            )
        return self


class OracleContract(StrictModel):
    source_commit: GitSHA
    patch: ArtifactRef
    preconditions: list[Nonempty] = Field(min_length=1)
    requirement_ids: list[Identifier] = Field(min_length=1)
    adaptations: list[Nonempty] = Field(default_factory=list)
    materialization_digest: Digest
    execution_evidence: list[Nonempty] = Field(default_factory=list)


class TaskBundle(StrictModel):
    task: TaskContract
    episode: EpisodeContract
    verifier: VerifierContract
    oracle: OracleContract
    materialization: MaterializationContract

    @model_validator(mode="after")
    def linked_sections(self) -> TaskBundle:
        required = {row.id for row in self.task.requirements}
        if (
            set(self.verifier.requirement_checks) != required
            or set(self.oracle.requirement_ids) != required
        ):
            raise ValueError("Verifier and oracle must map exactly the public requirement IDs")
        if self.episode.initial_source != self.task.source.before_commit:
            raise ValueError("Episode initial source differs from the frozen task source")
        if self.oracle.source_commit != self.task.source.reference_commit:
            raise ValueError("Oracle differs from the frozen reference source")
        if self.task.collection != self.materialization.collection:
            raise ValueError("Task and materialization collection manifests differ")
        if self.oracle.materialization_digest != digest(self.materialization):
            raise ValueError("Oracle references a different materialization procedure")
        return self


QUALITY_DIMENSIONS = (
    "useful_scope",
    "instruction_sufficiency",
    "verifier_correctness_coverage",
    "valid_alternative_tolerance",
    "oracle_validity",
    "isolation_leakage",
    "reproducibility_materialization",
    "trajectory_findings",
)


class QualityCriterion(StrictModel):
    status: Literal["pass", "fail", "incomplete", "not_applicable"]
    score: int | None = Field(ge=0, le=4, strict=True)
    explanation: Nonempty
    evidence_ids: list[Nonempty] = Field(default_factory=list)
    counterevidence: list[Nonempty] = Field(default_factory=list)
    uncertainty: list[Nonempty] = Field(default_factory=list)
    severity: Literal["none", "advisory", "material"] = "none"
    proposed_repair: Nonempty | None = None

    @model_validator(mode="after")
    def coherent_status(self) -> QualityCriterion:
        if self.status == "pass" and (
            self.score not in {3, 4} or not self.evidence_ids or self.severity == "material"
        ):
            raise ValueError("Pass requires score 3/4, evidence, and no material finding")
        if self.status == "fail" and self.score in {3, 4}:
            raise ValueError("A failing dimension cannot have a supported score")
        if self.status == "not_applicable" and (self.score is not None or not self.evidence_ids):
            raise ValueError("Not-applicable requires a null score and cited justification")
        return self


class Finding(StrictModel):
    id: Identifier
    explanation: Nonempty
    severity: Literal["advisory", "material"]
    resolved: bool = False
    evidence_ids: list[Nonempty] = Field(min_length=1)


class EvidenceRef(StrictModel):
    revision_digest: Digest
    artifact: ArtifactRef


TrialKind = Literal[
    "baseline",
    "oracle",
    "negative_control",
    "positive_control",
    "tamper",
    "independent_challenge",
    "solver",
    "adversary",
]


class TrialRecord(StrictModel):
    id: Identifier
    revision_digest: Digest
    kind: TrialKind
    outcome: Literal["passed", "submission_failure", "infrastructure_failure", "incomplete"]
    reward: int | None = Field(ge=0, le=1, strict=True)
    environment_healthy: bool
    collection_complete: bool
    materialization_verified: bool
    checks_run: int = Field(ge=0, strict=True)
    evidence_ids: list[Nonempty] = Field(min_length=1)
    diagnostic: Nonempty
    model: Nonempty | None = None

    @model_validator(mode="after")
    def healthy_reward(self) -> TrialRecord:
        if self.valid:
            expected = 1 if self.outcome == "passed" else 0
            if self.reward != expected or not (
                self.environment_healthy
                and self.collection_complete
                and self.materialization_verified
            ):
                raise ValueError(
                    "A behavioral reward requires healthy, complete collection/materialization evidence"
                )
            if self.outcome == "passed" and self.checks_run == 0:
                raise ValueError("Zero executed checks cannot establish a behavioral pass")
        elif self.reward is not None:
            raise ValueError("Infrastructure or incomplete evidence cannot be a training reward")
        return self

    @property
    def valid(self) -> bool:
        return self.outcome in {"passed", "submission_failure"}


class TrialRequirement(StrictModel):
    id: Identifier
    kind: TrialKind
    expected_reward: int | None = Field(ge=0, le=1, strict=True)
    model: Nonempty | None = None


def _quality_criteria_schema(schema: dict) -> None:
    # A Python field validator cannot communicate allowed dictionary keys to the
    # model. Reuse Pydantic's generated criterion reference so custom ref templates
    # and enclosing schemas keep their usual definition handling.
    criterion = schema["additionalProperties"]
    schema.update(
        properties={name: dict(criterion) for name in QUALITY_DIMENSIONS},
        required=list(QUALITY_DIMENSIONS),
        additionalProperties=False,
    )


class QualityReport(StrictModel):
    pr: PRIdentity
    revision_digest: Digest
    policy_version: Literal[1] = 1
    criteria: dict[str, QualityCriterion] = Field(json_schema_extra=_quality_criteria_schema)
    findings: list[Finding] = Field(default_factory=list)
    difficulty: str | None = None
    expert_review: Literal["not_reviewed", "reviewed"] = "not_reviewed"

    @field_validator("criteria")
    @classmethod
    def all_dimensions(cls, value: dict[str, QualityCriterion]) -> dict[str, QualityCriterion]:
        if set(value) != set(QUALITY_DIMENSIONS):
            raise ValueError("Quality report must contain exactly the eight policy dimensions")
        return value


class AdmissionContext(StrictModel):
    """Controller-frozen requirements and independently verified receipts, never author policy."""

    pr: PRIdentity
    revision_digest: Digest
    required_trials: list[TrialRequirement] = Field(min_length=1)
    trials: list[TrialRecord]
    evidence: dict[str, EvidenceRef]
    execution_profile_validated: bool
    artifact_integrity_verified: bool
    allowed_not_applicable: dict[str, Nonempty] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_inventory(self) -> AdmissionContext:
        for rows in (self.required_trials, self.trials):
            if len({row.id for row in rows}) != len(rows):
                raise ValueError(
                    "Trial IDs must be unique; retries are not additional required trials"
                )
        if set(self.allowed_not_applicable) - set(QUALITY_DIMENSIONS):
            raise ValueError("Unknown not-applicable policy dimension")
        return self


def admission_reasons(report: QualityReport, context: AdmissionContext) -> list[str]:
    """No aggregate score overrides a missing receipt or a mandatory failed dimension."""
    reasons = []
    if report.pr != context.pr or report.revision_digest != context.revision_digest:
        reasons.append("Quality report is for a different PR or revision")
    if not context.execution_profile_validated:
        reasons.append("Execution profile is not validated")
    if not context.artifact_integrity_verified:
        reasons.append("Artifact identities are not verified")

    def evidence(ids, label):
        for key in ids:
            reference = context.evidence.get(key)
            if reference is None or reference.revision_digest != context.revision_digest:
                reasons.append(f"{label}: missing or stale evidence {key}")

    for name, criterion in report.criteria.items():
        if criterion.status == "not_applicable":
            if name not in context.allowed_not_applicable:
                reasons.append(f"{name}: policy does not permit not-applicable")
        elif criterion.status != "pass" or criterion.score not in {3, 4}:
            reasons.append(f"{name}: required dimension is {criterion.status}")
        if criterion.severity == "material":
            reasons.append(f"{name}: unresolved material finding")
        evidence(criterion.evidence_ids, name)
    for finding in report.findings:
        if finding.severity == "material" and not finding.resolved:
            reasons.append(f"Unresolved material finding: {finding.id}")
        evidence(finding.evidence_ids, finding.id)
    trials = {row.id: row for row in context.trials}
    for required in context.required_trials:
        trial = trials.get(required.id)
        if trial is None:
            reasons.append(f"Missing required trial: {required.id}")
            continue
        if (
            trial.revision_digest != context.revision_digest
            or trial.kind != required.kind
            or trial.model != required.model
        ):
            reasons.append(f"Trial identity mismatch: {required.id}")
        if not trial.valid:
            reasons.append(f"Incomplete required trial: {required.id}")
        if required.expected_reward is not None and trial.reward != required.expected_reward:
            reasons.append(f"Unexpected control outcome: {required.id}")
        evidence(trial.evidence_ids, required.id)
    return list(dict.fromkeys(reasons))


class OperationKey(StrictModel):
    pr_id: Nonempty
    revision_digest: Digest
    stage: Identifier
    input_digest: Digest
    policy_digest: Digest
    attempt: int = Field(ge=0, strict=True)
    effect: Identifier

    @property
    def operation_id(self) -> str:
        return digest(self)

    @property
    def effect_id(self) -> str:
        return digest(self.model_dump(exclude={"attempt"}))


OperationStatus = Literal["claimed", "submitted", "completed", "failed", "uncertain"]


class OperationRecord(StrictModel):
    key: OperationKey
    status: OperationStatus
    ledger: LedgerRef
    deadline: Deadline
    external_id: str | None = None
    reservation_ids: list[str] = Field(default_factory=list)
    receipt: dict = Field(default_factory=dict)
    error: str | None = None
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)


class Lease(StrictModel):
    pr_id: Nonempty
    owner: Nonempty
    token: Nonempty
    expires_at: float
    deadline: Deadline


class OperationClaim(StrictModel):
    operation: OperationRecord
    acquired: bool
