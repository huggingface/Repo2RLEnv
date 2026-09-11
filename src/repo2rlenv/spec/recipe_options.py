"""Explicit Python repository profile for the procedural SWE-smith recipe."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PythonRepositoryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_paths: list[str] = Field(min_length=1)
    test_paths: list[str] = Field(min_length=1)
    base_image: str = "python:3.12-slim"
    dependencies: list[str] = Field(default_factory=lambda: ["pytest==9.0.3"])
    install_command: str = "python -m pip install --no-cache-dir -e ."
    test_timeout_sec: int = Field(default=90, ge=5, le=600)

    @field_validator("source_paths", "test_paths")
    @classmethod
    def safe_paths(cls, values: list[str]) -> list[str]:
        from repo2rlenv.emitter.bundle import relative_asset_path

        for value in values:
            relative_asset_path("environment/" + value)
            if value.startswith("-"):
                raise ValueError("Repository paths cannot be command options")
        return values

    @field_validator("base_image", "install_command", "dependencies")
    @classmethod
    def no_directive_injection(cls, value):
        items = value if isinstance(value, list) else [value]
        if any(not item.strip() or "\n" in item or "\r" in item for item in items):
            raise ValueError("Build options must be nonempty single-line values")
        return value


class SWESmithOptions(PythonRepositoryProfile):
    seed: int = 24
    max_candidates: int = Field(default=100, ge=1, le=1000)
    max_per_entity: int = Field(default=2, ge=1, le=10)
    target: int = Field(default=20, ge=1, le=1000)


class PRRecipeOptions(PythonRepositoryProfile):
    target: int = Field(default=20, ge=1, le=1000)
    max_candidates: int = Field(default=40, ge=1, le=1000)
    force_generate_instruction: bool = False


class ReconstructionOptions(PythonRepositoryProfile):
    target: int = Field(default=20, ge=1, le=1000)
    max_candidates: int = Field(default=60, ge=1, le=1000)
    trace_max_tests: int = Field(default=128, ge=20, le=1000)
    trace_seed: int = 24


class HistoryRecipeOptions(PythonRepositoryProfile):
    target: int = Field(default=20, ge=1, le=1000)
    max_candidates: int = Field(default=80, ge=1, le=1000)
    history_limit: int = Field(default=2000, ge=1, le=10000)
    max_non_test_files: int = Field(default=5, ge=1, le=30)
    max_non_test_edited_lines: int = Field(default=200, ge=1, le=2000)
    max_patch_length: int = Field(default=10000, ge=100, le=100000)
    require_bug_edit: bool = False
    require_test_match: bool = False
    max_added_entities: int = Field(default=1, ge=0, le=20)
    max_edited_entities: int = Field(default=4, ge=1, le=50)
    max_statement_entities: int = Field(default=6, ge=0, le=100)


class R2EGymOptions(HistoryRecipeOptions):
    require_bug_edit: bool = True
    require_test_match: bool = True


class SWENextOptions(HistoryRecipeOptions):
    max_prs: int = Field(default=300, ge=1, le=2000)
    pr_numbers: list[int] = Field(default_factory=list)

    @field_validator("pr_numbers")
    @classmethod
    def positive_unique_prs(cls, values):
        if any(number < 1 for number in values) or len(set(values)) != len(values):
            raise ValueError("PR numbers must be positive and unique")
        return values


class R2EOptions(PythonRepositoryProfile):
    target: int = Field(default=20, ge=1, le=1000)
    max_candidates: int = Field(default=60, ge=1, le=1000)
    max_rounds: int = Field(default=3, ge=1, le=5)
    min_branch_coverage: float = Field(default=0.8, ge=0, le=1)
    seed: int = 24
    dependencies: list[str] = Field(default_factory=lambda: ["pytest==9.0.3", "coverage==7.16.0"])


class EnvironmentRepairOptions(PythonRepositoryProfile):
    target: int = Field(default=20, ge=1, le=1000)
    max_candidates: int = Field(default=40, ge=1, le=1000)
    max_rounds: int = Field(default=3, ge=1, le=5)
    seed: int = 24
    directions: list[str] = Field(
        default_factory=lambda: [
            "Tamper with development environment configuration",
            "Disrupt package import resolution without editing repository source",
            "Break filesystem layout or links required by tests",
            "Disrupt installed dependencies",
            "Disrupt command entry points used by the development environment",
        ],
        min_length=1,
    )


class TerminalSynthesisOptions(BaseModel):
    """Bounds for terminal task authoring and its executable repair loop."""

    model_config = ConfigDict(extra="forbid")
    target: int = Field(default=20, ge=1, le=1000)
    max_candidates: int = Field(default=40, ge=1, le=2000)
    max_repairs: int = Field(default=2, ge=0, le=4)
    seed: int = 24
    max_tokens: int = Field(default=10000, ge=2048, le=16000)
    test_timeout_sec: int = Field(default=120, ge=10, le=600)


class RecordingReconstructionOptions(TerminalSynthesisOptions):
    min_score: int = Field(default=4, ge=0, le=12)


class DataArcOptions(TerminalSynthesisOptions):
    strategies: list[Literal["few_shot", "self_instruct", "evol_instruct"]] = Field(
        default_factory=lambda: ["few_shot", "self_instruct", "evol_instruct"], min_length=1
    )
    evol_directions: list[Literal["in_depth", "in_breadth"]] = Field(
        default_factory=lambda: ["in_depth", "in_breadth"], min_length=1
    )
    samples_per_strategy: int = Field(default=3, ge=1, le=100)


class TaskEvolutionOptions(TerminalSynthesisOptions):
    strategies: list[
        Literal[
            "increase_difficulty",
            "decrease_difficulty",
            "change_context",
            "increase_difficulty_and_change_context",
            "slight_increase",
            "slight_decrease",
        ]
    ] = Field(
        default_factory=lambda: ["increase_difficulty", "change_context", "decrease_difficulty"],
        min_length=1,
    )
    variants_per_parent: int = Field(default=1, ge=1, le=20)
