"""Explicit Python repository profile for the procedural SWE-smith recipe."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HubAsset(BaseModel):
    """Public model/tokenizer data fetched during remote image construction."""

    model_config = ConfigDict(extra="forbid")
    repo_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    filenames: list[str] = Field(min_length=1, max_length=64)
    max_bytes: int = Field(default=268435456, ge=1, le=2147483648)
    purpose: str = Field(min_length=10, max_length=500)
    cache_aliases: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("cache_aliases")
    @classmethod
    def cache_names(cls, values):
        import re

        if len(set(values)) != len(values):
            raise ValueError("Hub cache aliases must be unique")
        for value in values:
            parts = value.split("/")
            if (
                not re.fullmatch(
                    r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)?", value
                )
                or any(len(part) > 96 or part.endswith((".", "-", ".git")) for part in parts)
                or "--" in value
                or ".." in value
            ):
                raise ValueError("Hub cache aliases must be explicit valid repository IDs")
        return sorted(values)

    @field_validator("filenames")
    @classmethod
    def data_files(cls, values):
        from pathlib import PurePosixPath

        from repo2rlenv.emitter.bundle import relative_asset_path

        if len(values) != len(set(values)):
            raise ValueError("Hub asset filenames must be unique")
        for value in values:
            relative_asset_path("environment/assets/" + value)
            if PurePosixPath(value).is_absolute():
                raise ValueError("Hub filenames must be relative")
            if PurePosixPath(value).suffix not in {
                ".json",
                ".txt",
                ".model",
                ".tiktoken",
                ".jinja",
                ".safetensors",
                ".bin",
            } or any(symbol in value for symbol in "*?[]"):
                raise ValueError("List exact model/tokenizer data files, without code or globs")
        return sorted(values)


class PythonRepositoryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_paths: list[str] = Field(min_length=1)
    test_paths: list[str] = Field(min_length=1)
    private_test_paths: list[str] = Field(default_factory=list)
    pytest_args: list[str] = Field(default_factory=list)
    test_selectors: list[str] = Field(default_factory=list)
    public_exclude: list[str] = Field(default_factory=list)
    materialize_document_links: list[str] = Field(default_factory=list, max_length=32)
    base_image: str = "python:3.12-slim"
    use_system_site_packages: bool = False
    dependencies: list[str] = Field(default_factory=lambda: ["pytest==9.0.3"])
    hub_assets: list[HubAsset] = Field(default_factory=list, max_length=8)
    install_command: str = "python -m pip install --no-cache-dir -e ."
    task_install_command: str | None = None
    freeze_git_version: bool = False
    test_timeout_sec: int = Field(default=90, ge=5, le=600)
    exclude_candidate_ids: list[str] = Field(default_factory=list)
    test_cpus: int = Field(default=1, ge=1, le=16, strict=True)
    test_memory_mb: int = Field(default=2048, ge=128, le=65536, strict=True)

    @model_validator(mode="after")
    def pinned_asset_dependencies(self):
        if self.hub_assets:
            if not any(
                item.lower().replace("_", "-").startswith("huggingface-hub==")
                for item in self.dependencies
            ):
                raise ValueError("Hub assets require a compatible huggingface-hub== version pin")
            if len({asset.repo_id for asset in self.hub_assets}) != len(self.hub_assets):
                raise ValueError("Choose one pinned revision per model/tokenizer repository")
            cache_names = [
                name.replace("/", "--")
                for asset in self.hub_assets
                for name in [asset.repo_id, *asset.cache_aliases]
            ]
            if len(set(cache_names)) != len(cache_names):
                raise ValueError("Hub cache aliases and canonical repositories must not collide")
            if sum(asset.max_bytes for asset in self.hub_assets) > 2147483648:
                raise ValueError("Combined Hub asset allowance must not exceed 2 GiB")
        return self

    @field_validator(
        "source_paths",
        "test_paths",
        "private_test_paths",
        "public_exclude",
        "test_selectors",
        "materialize_document_links",
    )
    @classmethod
    def safe_paths(cls, values: list[str]) -> list[str]:
        from repo2rlenv.emitter.bundle import relative_asset_path

        for value in values:
            relative_asset_path("environment/" + value)
            if value.startswith("-"):
                raise ValueError("Repository paths cannot be command options")
        return values

    @field_validator("materialize_document_links")
    @classmethod
    def document_links_only(cls, values: list[str]) -> list[str]:
        from pathlib import PurePosixPath

        if len(set(values)) != len(values) or any(
            PurePosixPath(value).suffix.lower() not in {".md", ".rst", ".txt"} for value in values
        ):
            raise ValueError(
                "Document links must be unique Markdown, reStructuredText or text paths"
            )
        return values

    @field_validator(
        "base_image", "install_command", "task_install_command", "dependencies", "pytest_args"
    )
    @classmethod
    def no_directive_injection(cls, value):
        if value is None:
            return value
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


class CodeMidasOptions(PythonRepositoryProfile):
    """Paper-inspired construction with explicit, separately metered screening."""

    test_paths: list[str] = Field(default_factory=lambda: ["test_codemidas_generated.py"])
    pytest_args: list[str] = Field(default_factory=lambda: ["--noconftest"])
    target: int = Field(default=5, ge=1, le=1000)
    max_candidates: int = Field(default=12, ge=1, le=1000)
    max_per_module: int = Field(default=2, ge=1, le=100)
    min_implementation_statements: int = Field(default=5, ge=1, le=100)
    seed: int = 24
    stack_manifest: Path | None = None
    stack_materialization: Literal["inline", "hydrated"] = "inline"
    max_rounds: int = Field(default=3, ge=1, le=3)
    max_turns: int = Field(default=16, ge=4, le=40)
    candidate_budget_usd: float = Field(default=2.0, gt=0, le=20)
    author_model: Literal["openai/gpt-6-luna", "openai/gpt-6-sol"] = "openai/gpt-6-luna"
    reviewer_model: Literal["openai/gpt-6-luna", "openai/gpt-6-sol"] = "openai/gpt-6-sol"

    @model_validator(mode="after")
    def generated_test_contract(self):
        if self.test_paths != ["test_codemidas_generated.py"] or self.test_selectors:
            raise ValueError("CodeMidas runs only its independently constructed private verifier")
        return self


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
    review_drafts: bool = False
    exclude_seed_sha256: list[str] = Field(default_factory=list)


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


class ScalerOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: int = Field(default=20, ge=1, le=1000)
    max_candidates: int = Field(default=60, ge=1, le=1000)
    seed: int = 24
    difficulties: list[int] = Field(default_factory=lambda: [2, 4, 6, 8, 10, 12], min_length=1)
    samples_per_difficulty: int = Field(default=1, ge=1, le=100)
    execution_timeout_sec: int = Field(default=20, ge=1, le=120)
    max_generator_attempts: int = Field(default=3, ge=1, le=5)
    exclude_instance_hashes: list[str] = Field(default_factory=list)

    @field_validator("difficulties")
    @classmethod
    def nonnegative_unique_levels(cls, values):
        if any(level < 0 for level in values) or len(set(values)) != len(values):
            raise ValueError("Difficulty levels must be nonnegative and unique")
        return values


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
