from __future__ import annotations

import math
from itertools import product
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from repo2rlenv.curation.inference import inference_digest


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Requirement(StrictModel):
    id: str
    behavior: str
    tests: list[str] = Field(min_length=1)


class Mutation(StrictModel):
    name: str
    rationale: str
    script: str


class JudgeRewardSpec(StrictModel):
    justification: str = Field(min_length=30)
    criteria: dict[str, str] = Field(min_length=1)
    artifacts: list[str] = Field(min_length=1)
    threshold: float = Field(default=1, gt=0, le=1)


class Contract(StrictModel):
    title: str
    rationale: str
    source_paths: list[str] = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=2)
    mutations: list[Mutation] = Field(min_length=2)
    equivalents: list[Mutation] = Field(min_length=1)
    min_tests: int = Field(ge=3)
    reward_mode: Literal["deterministic", "judge"] = "deterministic"
    judge_justification: str | None = None
    judge_reward: JudgeRewardSpec | None = None

    @model_validator(mode="after")
    def unique_controls(self) -> Contract:
        for controls in (self.mutations, self.equivalents):
            if len({c.name for c in controls}) != len(controls):
                raise ValueError("Control names must be unique")
            if any(not c.script.strip() or not c.rationale.strip() for c in controls):
                raise ValueError("Controls need executable scripts and rationales")
        return self

    @field_validator("source_paths")
    @classmethod
    def safe_paths(cls, values: list[str]) -> list[str]:
        from pathlib import PurePosixPath

        for value in values:
            p = PurePosixPath(value)
            if (
                p.is_absolute()
                or ".." in p.parts
                or str(p) != value
                or not p.parts
                or any(x.startswith(".") for x in p.parts)
                or p.parts[0] in {"tests", "solution", "environment"}
            ):
                raise ValueError(f"Invalid submission path: {value}")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate submission paths")
        for a in values:
            for b in values:
                if a != b and PurePosixPath(a) in PurePosixPath(b).parents:
                    raise ValueError("Overlapping submission paths")
        return values


CRITERIA = {
    "task_specification": 20,
    "realism": 10,
    "test_coverage": 20,
    "verifier_integrity": 15,
    "solvability": 10,
    "reproducibility": 10,
    "intrinsic_difficulty": 10,
    "trace_quality": 5,
}
VALIDITY_CRITERIA = {
    name: weight for name, weight in CRITERIA.items() if name != "intrinsic_difficulty"
}
AcceptancePolicy = Literal["legacy", "validity"]


class Criterion(StrictModel):
    score: int = Field(ge=0, le=4)
    outcome: Literal["pass", "fail", "not_applicable"]
    explanation: str = Field(min_length=20)
    evidence: list[str] = Field(min_length=1)


class SpecificationReview(StrictModel):
    """Historical static feedback; current preflight uses the stricter subclass below."""

    score: int = Field(ge=0, le=4, strict=True)
    blockers: list[str]
    repairs: list[str]
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def actionable_findings(self) -> SpecificationReview:
        if any(not item.strip() for item in (*self.blockers, *self.repairs, *self.evidence)):
            raise ValueError("Specification review entries must contain text")
        if not self.passed and not self.repairs:
            raise ValueError("A failed specification review needs concrete repairs")
        return self

    @property
    def passed(self) -> bool:
        return self.score >= 3 and not self.blockers


class SpecificationPreflightReview(SpecificationReview):
    """Required specification corrections cannot be overridden by a high score."""

    repairs: list[str] = Field(
        description="Required substantive specification corrections; any entry prevents passing."
    )
    optional_improvements: list[str] = Field(
        description=(
            "Nonblocking polish only; use an empty list when none. Style preferences or extra "
            "repetition of already established semantics are optional. Hidden graded behavior, "
            "material ambiguity, and material solution leakage belong in repairs."
        )
    )

    @model_validator(mode="after")
    def optional_findings_contain_text(self) -> SpecificationPreflightReview:
        if any(not item.strip() for item in self.optional_improvements):
            raise ValueError("Optional specification improvements must contain text")
        return self

    @property
    def passed(self) -> bool:
        return super().passed and not self.repairs


class AuthorityEvidence(StrictModel):
    path: str
    line: int | None = Field(
        default=None,
        ge=1,
        strict=True,
        description="Optional exact one-based line; omitted lines are resolved from raw quotes, using the mapped test/helper for distinguished test evidence",
    )
    quote: str = Field(
        min_length=1,
        description="Exact raw source text, not a paraphrase; repeated public-contract text is permitted",
    )


class InputAuthorityCheck(StrictModel):
    requirement_id: str
    authoritative_input: str | None
    competing_input: str | None
    public_condition: str = Field(min_length=10)
    discordant_fixture: str | None
    expected_observation: str | None
    conditional_shortcut: str | None
    distinguishing_test: str | None = Field(
        pattern=r"^test_[A-Za-z0-9_]+$",
        description="Exactly one mapped protected test function name, never joined names; null when no distinguishing test is claimed",
    )
    result: Literal["distinguished", "gap", "not_applicable"]
    reason: str = Field(min_length=30)
    evidence: list[AuthorityEvidence] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def concrete_challenge(self) -> InputAuthorityCheck:
        if (
            not self.requirement_id.strip()
            or not self.public_condition.strip()
            or not self.reason.strip()
        ):
            raise ValueError("Input authority checks require identifiers and grounded explanations")
        if self.result != "not_applicable":
            for name in (
                "authoritative_input",
                "competing_input",
                "discordant_fixture",
                "expected_observation",
                "conditional_shortcut",
            ):
                value = getattr(self, name)
                if value is None or not value.strip():
                    raise ValueError(f"Applicable input authority check requires {name}")
        if self.result == "distinguished" and not (self.distinguishing_test or "").strip():
            raise ValueError("Distinguished input authority check needs a protected test")
        return self


class VerifierReview(SpecificationReview):
    """A high score cannot override an outstanding verifier repair request."""

    repairs: list[str] = Field(description="Required corrections; any entry prevents passing.")
    # Retained historical records remain readable. The current preflight uses
    # VerifierPreflightReview below, where the worksheet is mandatory.
    authority_checks: list[InputAuthorityCheck] | None = None
    optional_improvements: list[str] = Field(
        default_factory=list,
        description=(
            "Nonblocking polish only. A concrete uncovered contract violation or a valid "
            "implementation rejected by the tests belongs in blockers and repairs."
        ),
    )

    @model_validator(mode="after")
    def optional_findings_contain_text(self) -> VerifierReview:
        if any(not item.strip() for item in self.optional_improvements):
            raise ValueError("Optional verifier improvements must contain text")
        return self

    @property
    def passed(self) -> bool:
        return (
            super().passed
            and not self.repairs
            and not any(check.result == "gap" for check in self.authority_checks or [])
        )


class ConditionAxis(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,49}$")
    values: list[str] = Field(min_length=2, max_length=4)
    public_meaning: str = Field(min_length=20)
    evidence: list[AuthorityEvidence] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def distinct_values(self) -> ConditionAxis:
        if any(not value.strip() for value in self.values) or len(set(self.values)) != len(
            self.values
        ):
            raise ValueError("Condition axis values must be distinct nonempty public categories")
        return self


class ConditionCase(StrictModel):
    values: dict[str, str] = Field(min_length=2, max_length=4)
    result: Literal["covered", "gap", "inapplicable"]
    fixture_input: str | None
    expected_observation: str | None
    test: str | None = Field(default=None, pattern=r"^test_[A-Za-z0-9_]+$")
    fixture_evidence: list[AuthorityEvidence] = Field(default_factory=list, max_length=6)
    expected_evidence: list[AuthorityEvidence] = Field(default_factory=list, max_length=6)
    inapplicable_evidence: list[AuthorityEvidence] = Field(default_factory=list, max_length=4)
    reason: str = Field(min_length=30)

    @model_validator(mode="after")
    def concrete_case(self) -> ConditionCase:
        if self.result != "inapplicable" and (
            not (self.fixture_input or "").strip() or not (self.expected_observation or "").strip()
        ):
            raise ValueError(
                "A covered or missing joint case needs concrete inputs and an independent expected observation"
            )
        if self.result == "covered" and (
            not self.test or not self.fixture_evidence or not self.expected_evidence
        ):
            raise ValueError(
                "Covered joint case requires a mapped test, fixture evidence and expected-observation evidence"
            )
        if self.result == "inapplicable" and not self.inapplicable_evidence:
            raise ValueError(
                "Inapplicable combination needs cited public scope or an impossibility justification"
            )
        if self.result != "inapplicable" and self.inapplicable_evidence:
            raise ValueError("Only inapplicable combinations may carry inapplicable evidence")
        return self


class ConditionMatrix(StrictModel):
    requirement_ids: list[str] = Field(min_length=1, max_length=64)
    interaction_reason: str = Field(
        min_length=30,
        description="Why these independently varying public conditions materially interact, or a grounded reason no material interaction exists when axes/cases are empty.",
    )
    evidence: list[AuthorityEvidence] = Field(min_length=1, max_length=6)
    axes: list[ConditionAxis] = Field(max_length=4)
    cases: list[ConditionCase] = Field(max_length=32)

    @model_validator(mode="after")
    def complete_cartesian_inventory(self) -> ConditionMatrix:
        if any(not name.strip() for name in self.requirement_ids) or len(
            set(self.requirement_ids)
        ) != len(self.requirement_ids):
            raise ValueError("Condition matrix requires distinct nonempty requirement IDs")
        if not self.axes:
            if self.cases:
                raise ValueError("No-interaction assessment must have no cases")
            return self
        names = [axis.name for axis in self.axes]
        if len(names) < 2 or len(set(names)) != len(names):
            raise ValueError("A joint condition matrix needs two to four distinct axes")
        expected = set(product(*(axis.values for axis in self.axes)))
        if len(expected) > 32:
            raise ValueError(
                "Condition matrix exceeds 32 joint cases; keep only grounded material interaction groups"
            )
        actual = []
        for case in self.cases:
            if set(case.values) != set(names):
                raise ValueError(f"Joint case must specify exactly these axes: {names}")
            values = tuple(case.values[name] for name in names)
            if values not in expected:
                raise ValueError(f"Joint case has undeclared axis values: {values}")
            actual.append(values)
        if len(set(actual)) != len(actual):
            raise ValueError("Duplicate joint condition combination")
        missing = sorted(expected - set(actual))
        if missing:
            raise ValueError(
                f"Condition matrix omits joint combinations for {names}: {missing}; mark each covered, gap, or justified inapplicable"
            )
        return self


class VerifierPreflightReview(VerifierReview):
    """Retained policy-10 preflight schema, without joint-condition requirements."""

    authority_checks: list[InputAuthorityCheck] = Field(min_length=1, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def authority_gaps_are_required_repairs(cls, value):
        if not isinstance(value, dict):
            return value
        checks = [
            InputAuthorityCheck.model_validate(row) for row in value.get("authority_checks") or []
        ]
        gaps = [row for row in checks if row.result == "gap"]
        if not gaps:
            return value
        value = dict(value)
        if not isinstance(value.get("repairs"), list):
            return value  # Preserve ordinary schema errors instead of coercing them.
        repairs = list(value["repairs"])
        for row in gaps:
            repair = (
                f"Input authority gap [{row.requirement_id}] under {row.public_condition}: "
                f"{row.reason} Add a distinguishing observation for {row.discordant_fixture}; "
                f"expected {row.expected_observation}. Shortcut to reject: {row.conditional_shortcut}."
            )
            if repair not in repairs:
                repairs.append(repair)
        value["repairs"] = repairs
        if type(value.get("score")) is int:
            value["score"] = min(value["score"], 2)
        return value


class VerifierPreflightReviewV11(VerifierPreflightReview):
    """Current preflight inventory; historical and final admission schemas stay unchanged."""

    condition_matrices: list[ConditionMatrix] = Field(min_length=1, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def joint_gaps_are_required_repairs(cls, value):
        if not isinstance(value, dict):
            return value
        matrices = [
            ConditionMatrix.model_validate(row) for row in value.get("condition_matrices") or []
        ]
        if sum(len(matrix.cases) for matrix in matrices) > 64:
            raise ValueError("Joint condition inventory exceeds 64 total cases")
        gaps = [
            (matrix, case) for matrix in matrices for case in matrix.cases if case.result == "gap"
        ]
        if not gaps or not isinstance(value.get("repairs"), list):
            return value
        value = dict(value)
        repairs = list(value["repairs"])
        for matrix, case in gaps:
            condition = ", ".join(f"{name}={item}" for name, item in sorted(case.values.items()))
            repair = (
                f"Joint condition gap [{', '.join(matrix.requirement_ids)}] under {condition}: "
                f"{case.reason} Add a distinguishing observation for {case.fixture_input}; "
                f"expected {case.expected_observation}."
            )
            if repair not in repairs:
                repairs.append(repair)
        value["repairs"] = repairs
        if type(value.get("score")) is int:
            value["score"] = min(value["score"], 2)
        return value

    @property
    def passed(self) -> bool:
        return super().passed and not any(
            case.result == "gap" for matrix in self.condition_matrices for case in matrix.cases
        )


class Review(StrictModel):
    criteria: dict[str, Criterion]
    blockers: list[str]
    failure_attribution: dict[
        str,
        Literal[
            "solved",
            "reasoning",
            "underspecified",
            "verifier",
            "infrastructure",
            "timeout",
            "unknown",
        ],
    ] = Field(
        description=(
            "Attribution for solver-* trials only. Describe expected control failures in "
            "criterion evidence and classify the adversary with adversary_assessment."
        )
    )
    reward_hacks: list[str]
    suggested_repairs: list[str]
    adversary_assessment: Literal["attempted_hack", "solved_task", "no_audit", "unknown"] = Field(
        default="unknown",
        description=(
            "Classify the observed adversary behavior: attempted_hack means a concrete bypass "
            "attempt, successful or not; solved_task means legitimate implementation; no_audit "
            "means no substantive attack attempt; unknown means insufficient evidence. "
            "A positive reward alone does not establish a confirmed reward hack."
        ),
    )

    @field_validator("criteria")
    @classmethod
    def complete_rubric(cls, value: dict[str, Criterion]) -> dict[str, Criterion]:
        if set(value) != set(CRITERIA):
            raise ValueError("Review must cover every criterion exactly once")
        return value

    @property
    def score(self) -> float:
        """Historical eight-criterion score; never reinterpret retained reviews."""
        return sum(CRITERIA[k] * v.score / 4 for k, v in self.criteria.items())

    @property
    def validity_score(self) -> float:
        """Normalize the seven validity criteria to the same 0–100 scale."""
        weighted = sum(
            weight * self.criteria[name].score for name, weight in VALIDITY_CRITERIA.items()
        )
        return 100 * weighted / (4 * sum(VALIDITY_CRITERIA.values()))

    def admission_score(self, policy: AcceptancePolicy) -> float:
        if policy == "legacy":
            return self.score
        if policy == "validity":
            return self.validity_score
        raise ValueError(f"Unknown acceptance policy: {policy}")


class TrialEvidence(StrictModel):
    label: str
    task_digest: str
    reward: float | None = None
    error: str | None = None
    path: str
    model: str | None = None
    inference_digest: str | None = None
    cost_usd: float = Field(default=0, ge=0)

    @property
    def valid(self) -> bool:
        return self.error is None and self.reward is not None and math.isfinite(self.reward)


class CampaignConfig(StrictModel):
    target: int = Field(default=30, ge=1)
    concurrency: int = Field(default=2, ge=1, le=4)
    budget_usd: float = Field(default=450, gt=0, le=500)
    author_model: str = "anthropic/claude-sonnet-4-6"
    author_runtime: Literal["langgraph", "pi", "opencode"] = "langgraph"
    judge_model: str = "anthropic/claude-opus-4-6"
    specification_review: bool = False
    verifier_review: bool = False
    solver_models: list[str] = Field(
        default_factory=lambda: ["anthropic/claude-sonnet-4-6", "anthropic/claude-opus-4-6"],
        min_length=2,
    )
    author_turns: int = Field(default=45, ge=1, le=100)
    solver_turns: int = Field(default=35, ge=1, le=100)
    max_revisions: int = Field(default=3, ge=1, le=8)
    max_candidate_drafts: int | None = Field(default=None, ge=1, le=8)
    require_verification_plan: bool = False
    acceptance_policy: Literal["legacy", "validity"] = "legacy"
    submission_policy: Literal["legacy", "conversion"] = "legacy"
    max_mechanical_submissions: int = Field(default=6, ge=1, le=20)
    oracle_repeats: int = Field(default=3, ge=2, le=20)
    solver_attempts: int = Field(default=1, ge=1, le=10)
    acceptance_score: float = Field(default=85, ge=0, le=100)
    max_candidate_usd: float = Field(default=18, gt=0)
    trial_timeout_sec: int = Field(default=900, ge=60, le=1800)
    author_timeout_sec: int = Field(default=3600, ge=60, le=7200)
    release_author_before_validation: bool = False
    cloud_trial_allowance_usd: float = Field(default=1.0, ge=0.5)
    author_cloud_allowance_usd: float = Field(default=1.5, ge=1)

    @model_validator(mode="after")
    def bounded_conversion(self) -> CampaignConfig:
        if self.submission_policy == "conversion" and (
            self.max_candidate_drafts is None
            or not self.require_verification_plan
            or not self.specification_review
            or not self.verifier_review
        ):
            raise ValueError(
                "Conversion policy requires a finite semantic draft limit, a design and both static reviews"
            )
        return self

    @field_validator("solver_models")
    @classmethod
    def distinct_models(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("Solver models must be distinct")
        return values


def execution_gate_reasons(
    trials: list[TrialEvidence],
    config: CampaignConfig,
    digest: str,
    mutation_names: list[str],
    equivalent_names: list[str],
) -> list[str]:
    """Check execution evidence independently of the specification review."""
    reasons = []
    if len({t.label for t in trials}) != len(trials):
        reasons.append("Duplicate trial labels")
    by_label = {t.label: t for t in trials}
    expected = {f"oracle-{i}": 1 for i in range(config.oracle_repeats)}
    expected.update({"baseline": 0, "tamper": 0, "pytest-tamper": 0})
    expected.update({f"mutation-{n}": 0 for n in mutation_names})
    expected.update({f"equivalent-{n}": 1 for n in equivalent_names})
    if not equivalent_names:
        reasons.append("No alternative valid implementation was tested")
    for label, reward in expected.items():
        t = by_label.get(label)
        if t is None or not t.valid or t.reward != reward or t.task_digest != digest:
            reasons.append(f"{label}: missing, stale, errored, or unexpected reward")
    for model in config.solver_models:
        matching = [t for t in trials if t.model == model and t.label.startswith("solver-")]
        if len(matching) < config.solver_attempts or any(
            not t.valid or t.task_digest != digest or t.inference_digest != inference_digest(model)
            for t in matching
        ):
            reasons.append(f"Incomplete solver evidence: {model}")
    attack = by_label.get("adversary")
    if (
        attack is None
        or not attack.valid
        or attack.reward != 0
        or attack.task_digest != digest
        or attack.model != config.author_model
        or attack.inference_digest != inference_digest(config.author_model, adversary=True)
    ):
        reasons.append("Adversarial trial missing, errored, stale, or achieved reward")
    return reasons


def review_scores(review: Review, config: CampaignConfig) -> dict[str, str | float]:
    """Explicit result metadata: active admission score and both comparable scales."""
    return {
        "score": review.admission_score(config.acceptance_policy),
        "legacy_score": review.score,
        "validity_score": review.validity_score,
        "intrinsic_difficulty_score": review.criteria["intrinsic_difficulty"].score,
        "acceptance_policy": config.acceptance_policy,
    }


def validate_review_scores(row: dict[str, Any], review: Review, config: CampaignConfig) -> None:
    """Validate a persisted score receipt without reclassifying legacy records."""
    if row.get("acceptance_policy", "legacy") != config.acceptance_policy:
        raise ValueError("Accepted result acceptance policy mismatch")
    for name, expected in review_scores(review, config).items():
        if name == "acceptance_policy":
            continue
        # Old legacy records carried only score. New auxiliary scores, when
        # present, must agree; validity admissions require the complete receipt.
        if name != "score" and config.acceptance_policy == "legacy" and name not in row:
            continue
        value = row.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or value != expected
        ):
            raise ValueError(f"Accepted result {name} mismatch")


def quality_gate_reasons(review: Review, config: CampaignConfig) -> list[str]:
    """Quality findings only; incomplete execution/audit evidence is separate."""
    reasons = []
    score = review.admission_score(config.acceptance_policy)
    if score < config.acceptance_score:
        label = "Quality" if config.acceptance_policy == "legacy" else "Validity"
        reasons.append(f"{label} score {score:.1f} below {config.acceptance_score}")
    reasons.extend(review.blockers)
    reasons.extend(f"Reward hack: {h}" for h in review.reward_hacks)
    for name, criterion in review.criteria.items():
        if config.acceptance_policy == "validity" and name == "intrinsic_difficulty":
            continue
        if criterion.outcome != "pass" or criterion.score < 3:
            reasons.append(f"Criterion not passed: {name}")
    return reasons


def acceptance(
    trials: list[TrialEvidence],
    review: Review,
    config: CampaignConfig,
    digest: str,
    mutation_names: list[str],
    equivalent_names: list[str],
) -> list[str]:
    """Return all rejection reasons. Missing evidence never implicitly passes."""
    reasons = execution_gate_reasons(trials, config, digest, mutation_names, equivalent_names)
    reasons.extend(quality_gate_reasons(review, config))
    if review.adversary_assessment != "attempted_hack":
        reasons.append(f"Incomplete adversarial audit: {review.adversary_assessment}")
    for t in trials:
        if t.label.startswith("solver-"):
            attribution = review.failure_attribution.get(t.label)
            if attribution not in ({"solved"} if t.reward == 1 else {"reasoning"}):
                reasons.append(f"Unresolved solver attribution: {t.label}")
    return reasons
