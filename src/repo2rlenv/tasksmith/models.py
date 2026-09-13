"""Small, explicit contracts between Tasksmith stages."""

from __future__ import annotations

from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repo2rlenv.quality.loop.models import LoopOptions
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Profile(Record):
    reasoning: str = Field(min_length=20)
    resource: Literal["cpu"]
    options: PythonRepositoryProfile
    dependency_inputs: list[str] = Field(min_length=1)
    upstream_test_rationale: str = Field(min_length=20)

    @model_validator(mode="after")
    def offline_profile(self):
        if not self.options.test_selectors:
            raise ValueError("Select explicit offline pytest files or node IDs")
        hidden = [PurePosixPath(value) for value in self.options.test_paths]
        for selector in self.options.test_selectors:
            path = PurePosixPath(selector.split("::", 1)[0])
            if not any(path == root or root in path.parents for root in hidden):
                raise ValueError("Every test selector must be inside a private test path")
        for item in self.dependency_inputs:
            if PurePosixPath(item).is_absolute() or ".." in PurePosixPath(item).parts:
                raise ValueError("Dependency inputs must be repository-relative paths")
        return self


class Requirement(Record):
    behavior: str = Field(min_length=10)
    verification: str = Field(min_length=10)
    source_evidence: str = Field(min_length=5)


class Design(Record):
    instruction: str = Field(min_length=100, max_length=12000)
    requirements: list[Requirement] = Field(min_length=1, max_length=10)
    strategy: Literal["pr_regression"] = "pr_regression"
    verifier_rationale: str = Field(min_length=20)
    upstream_test_policy: Literal["retain", "replace"] = "retain"
    # The private file supplements or replaces the selected grading suite. Its
    # fixed path cannot overwrite upstream files; bootstrap readiness is unchanged.
    additional_tests: str = Field(default="", max_length=24000)
    wrong_solution_ideas: list[str] = Field(min_length=1, max_length=4)
    valid_alternative_ideas: list[str] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def replacement_requires_tests(self):
        if self.upstream_test_policy == "replace" and not self.additional_tests.strip():
            raise ValueError("Replacing the upstream grading selection requires private tests")
        return self


class Panel(Record):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    prs: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_inputs(self):
        if len(set(self.prs)) != len(self.prs):
            raise ValueError("Panel inputs must be unique")
        return self


class BootstrapHint(Record):
    ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_image: str
    dependencies: list[str]
    scope: str

    @model_validator(mode="after")
    def build_fields(self):
        for value in [self.base_image, *self.dependencies]:
            if not value.strip() or "\n" in value or "\r" in value:
                raise ValueError("Bootstrap build hints must contain nonempty single-line values")
        return self


class Options(Record):
    provider: Literal["modal", "daytona"] = "modal"
    author_runtime: Literal["pi", "opencode"] = "pi"
    author_model: str = "anthropic/claude-sonnet-4-6"
    author_turns: int = Field(default=18, ge=2, le=50)
    author_stage_usd: str = "4.00"
    max_spend_usd: str = "100.00"
    worker_reservation_usd: str = "12.00"
    worker_cpus: int = Field(default=2, ge=1, le=16)
    worker_memory_mb: int = Field(default=4096, ge=1024, le=65536)
    worker_snapshot: str | None = Field(default=None, pattern=r"^im-[A-Za-z0-9]+$")
    bootstrap_hints: dict[str, BootstrapHint] = Field(default_factory=dict)
    max_stage_attempts: int = Field(default=3, ge=1, le=5)
    quality: LoopOptions = Field(
        default_factory=lambda: LoopOptions(
            repair=True,
            run_rollout=True,
            max_repairs=2,
            max_probes=4,
            max_turns=20,
            max_spend_usd="20.00",
        )
    )

    @model_validator(mode="after")
    def limits(self):
        if self.worker_snapshot and self.provider != "modal":
            raise ValueError("Worker snapshots currently require Modal")
        if not self.author_model.startswith("anthropic/"):
            raise ValueError(
                "Pi/OpenCode author bridge currently supports Anthropic; quality models may use OpenAI or Anthropic"
            )
        for value in (self.author_stage_usd, self.max_spend_usd, self.worker_reservation_usd):
            if not Decimal(value).is_finite() or Decimal(value) <= 0:
                raise ValueError("Spending limits must be finite and positive")
        return self
