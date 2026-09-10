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


class R2EOptions(PythonRepositoryProfile):
    target: int = Field(default=20, ge=1, le=1000)
    max_candidates: int = Field(default=60, ge=1, le=1000)
    max_rounds: int = Field(default=3, ge=1, le=5)
    min_branch_coverage: float = Field(default=0.8, ge=0, le=1)
    seed: int = 24
    dependencies: list[str] = Field(default_factory=lambda: ["pytest==9.0.3", "coverage==7.16.0"])


class TerminalSynthesisOptions(BaseModel):
    """Bounds for terminal task authoring and its executable repair loop."""

    model_config = ConfigDict(extra="forbid")
    target: int = Field(default=20, ge=1, le=1000)
    max_candidates: int = Field(default=40, ge=1, le=2000)
    max_repairs: int = Field(default=2, ge=0, le=4)
    seed: int = 24
    max_tokens: int = Field(default=10000, ge=2048, le=16000)
    test_timeout_sec: int = Field(default=120, ge=10, le=600)


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
