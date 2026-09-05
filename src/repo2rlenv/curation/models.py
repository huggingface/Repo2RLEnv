from __future__ import annotations

import math
from typing import Literal

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


class Criterion(StrictModel):
    score: int = Field(ge=0, le=4)
    outcome: Literal["pass", "fail", "not_applicable"]
    explanation: str = Field(min_length=20)
    evidence: list[str] = Field(min_length=1)


class SpecificationReview(StrictModel):
    """Static repair feedback, never a substitute for the admission review."""

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
        return sum(CRITERIA[k] * v.score / 4 for k, v in self.criteria.items())


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
    oracle_repeats: int = Field(default=3, ge=2, le=20)
    solver_attempts: int = Field(default=1, ge=1, le=10)
    acceptance_score: float = Field(default=85, ge=0, le=100)
    max_candidate_usd: float = Field(default=18, gt=0)
    trial_timeout_sec: int = Field(default=900, ge=60, le=1800)
    author_timeout_sec: int = Field(default=3600, ge=60, le=7200)
    cloud_trial_allowance_usd: float = Field(default=1.0, ge=0.5)
    author_cloud_allowance_usd: float = Field(default=1.5, ge=1)

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


def quality_gate_reasons(review: Review, config: CampaignConfig) -> list[str]:
    """Quality findings only; incomplete execution/audit evidence is separate."""
    reasons = []
    if review.score < config.acceptance_score:
        reasons.append(f"Quality score {review.score:.1f} below {config.acceptance_score}")
    reasons.extend(review.blockers)
    reasons.extend(f"Reward hack: {h}" for h in review.reward_hacks)
    for name, criterion in review.criteria.items():
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
