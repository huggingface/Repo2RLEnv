"""Acceptance requires all declared hard checks, never an average review score."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CheckName = Literal[
    "build",
    "contrast",
    "oracle_repeat",
    "regression",
    "isolation",
    "verifier_attacks",
    "specification",
    "blind_sonnet",
    "blind_opus",
]
REQUIRED_CHECKS = frozenset(
    {
        "build",
        "contrast",
        "oracle_repeat",
        "regression",
        "isolation",
        "verifier_attacks",
        "specification",
        "blind_sonnet",
        "blind_opus",
    }
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pass", "fail", "unknown", "not_applicable"]
    explanation: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_evidence(self) -> CheckResult:
        if self.status in {"pass", "fail"} and not self.evidence:
            raise ValueError("A completed check requires artifact evidence")
        return self


class ReviewScores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clarity: int | None = Field(default=None, ge=0, le=4)
    realism: int | None = Field(default=None, ge=0, le=4)
    coverage: int | None = Field(default=None, ge=0, le=4)
    robustness: int | None = Field(default=None, ge=0, le=4)
    # Corpus diversity is measured separately; a single-task reviewer cannot
    # establish population diversity from one instruction.


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    bundle_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    recipe: str
    recipe_version: str
    profile: Literal["coding", "reasoning"] = "coding"
    checks: dict[CheckName, CheckResult] = Field(default_factory=dict)
    scores: ReviewScores = Field(default_factory=ReviewScores)
    solver_success: dict[str, bool | None] = Field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        if self.checks.keys() != REQUIRED_CHECKS:
            return False
        for name, result in self.checks.items():
            if result.status == "pass" and result.evidence:
                continue
            # Reasoning instances do not have pre-existing repository regressions.
            if (
                name == "regression"
                and self.profile == "reasoning"
                and result.status == "not_applicable"
            ):
                continue
            return False
        return True

    def current_for(self, bundle_hash: str) -> bool:
        return self.bundle_hash == bundle_hash
