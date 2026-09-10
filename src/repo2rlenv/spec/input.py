"""Top-level input contract — same shape across every pipeline."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PipelineName(StrEnum):
    # Mined from upstream history
    PR_DIFF = "pr_diff"  # text-only PR mining (was: pr_mining_lite)
    PR_RUNTIME = "pr_runtime"  # PR mining w/ sandbox verification (was: pr_mining)
    COMMIT_RUNTIME = "commit_runtime"  # commit-level mining w/ sandbox (was: commit_mining)
    CVE_PATCHES = "cve_patches"  # CVE patches as training data (was: cve_mining)
    # Synthesized by LLM
    CODE_INSTRUCT = "code_instruct"  # OSS-Instruct-style (was: oss_instruct)
    EQUIVALENCE_TESTS = "equivalence_tests"
    # Owned recipes; individual implementations declare their availability.
    PR_TO_ENV = "pr_to_env"
    REPO_MUTATE = "repo_mutate"
    REPO_RECONSTRUCT = "repo_reconstruct"
    TERMINAL_SYNTH = "terminal_synth"
    TERMINAL_RECONSTRUCT = "terminal_reconstruct"
    TASK_EVOLVE = "task_evolve"
    ENV_REPAIR = "env_repair"
    REASONING_SYNTH = "reasoning_synth"


class RepoSpec(BaseModel):
    url: str
    ref: str = "HEAD"
    access: Literal["public", "private", "auto"] = "auto"
    auth_token_env: str | None = None
    sparse_paths: list[str] | None = None

    @field_validator("url")
    @classmethod
    def normalize_url(cls, v: str) -> str:
        v = v.strip()
        # Local checkout — canonicalize to an absolute file:// URL.
        if v.startswith("file://"):
            return "file://" + str(Path(v[len("file://") :]).expanduser().resolve())
        if v.startswith(("/", "~", "./", "../")):
            return "file://" + str(Path(v).expanduser().resolve())
        # Remote git host (github default for a bare owner/name; gitlab needs
        # a full URL since a bare owner/name is indistinguishable from github).
        if "/" not in v:
            raise ValueError(
                f"repo url must be 'owner/name', a full git URL, or a local path, got {v!r}"
            )
        if not v.startswith(("http://", "https://", "git@")):
            v = f"https://github.com/{v}"
        return v.rstrip("/").removesuffix(".git")

    @property
    def source_kind(self):  # -> sources.SourceKind
        from repo2rlenv.sources import detect_source_kind

        return detect_source_kind(self.url)

    @property
    def owner_name(self) -> tuple[str, str]:
        """Return (owner, name). Local repos get a synthetic ('local', <dir>)."""
        u = self.url
        if u.startswith("file://"):
            return "local", Path(u[len("file://") :]).name
        path = (
            u.replace("https://github.com/", "")
            .replace("git@github.com:", "")
            .replace("https://gitlab.com/", "")
            .replace("git@gitlab.com:", "")
        )
        parts = path.rstrip("/").split("/")
        if len(parts) < 2:
            raise ValueError(f"cannot parse owner/name from {self.url!r}")
        return parts[-2], parts[-1]


class RepositorySource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["repository"] = "repository"
    repo: RepoSpec


class PRSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["pr"] = "pr"
    urls: list[str] = Field(min_length=1)

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, values: list[str]) -> list[str]:
        import re

        values = [value.rstrip("/") for value in values]
        pattern = (
            r"https://(?:github\.com/[^/]+/[^/]+/pull/\d+|gitlab\.com/.+/-/merge_requests/\d+)/?"
        )
        if any(re.fullmatch(pattern, value) is None for value in values):
            raise ValueError("PR sources require explicit GitHub PR or GitLab MR HTTPS URLs")
        if len(set(values)) != len(values):
            raise ValueError("PR sources contain duplicate URLs")
        return values


class SeedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["seeds"] = "seeds"
    path: Path


class TaskSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["task"] = "task"
    path: Path


class RecordingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["recording"] = "recording"
    path: Path


class FamilySource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["family"] = "family"
    path: Path


SourceSpec = Annotated[
    RepositorySource | PRSource | SeedSource | TaskSource | RecordingSource | FamilySource,
    Field(discriminator="kind"),
]


class LLMSpec(BaseModel):
    provider: str
    model: str
    api_key_env: str | None = None
    endpoint: str | None = None
    max_concurrent: int = 5
    timeout_sec: int = 120
    fallback: LLMSpec | None = None

    @property
    def qualified_name(self) -> str:
        """LiteLLM-compatible 'provider/model' string."""
        # Some providers (huggingface) use a different format
        if self.provider in ("openai", "anthropic", "huggingface"):
            return f"{self.provider}/{self.model}"
        return f"{self.provider}/{self.model}"


class QALayer(StrEnum):
    DETERMINISM = "determinism"
    ORACLE_CONSISTENCY = "oracle_consistency"
    LLM_JUDGE = "llm_judge"
    FALSE_NEGATIVE = "false_negative"
    DIFF_PARSE = "diff_parse"  # lite-pipeline-only: oracle diff must apply cleanly


class QASpec(BaseModel):
    enabled: bool = True
    layers: list[QALayer] = Field(default_factory=lambda: [QALayer.DIFF_PARSE])
    judge_llm: LLMSpec | None = None
    determinism_runs: int = 3
    oracle_runs: int = 3
    skip_on_fail: bool = True


class SandboxSpec(BaseModel):
    """Sandbox config — describes what a pipeline needs at generation time.

    Repo2RLEnv ships NO sandbox runtime. This spec just records what the
    pipeline requests; the dispatch happens externally:

    - `provider="none"` (default for lite pipelines) — no execution at gen
      time. Lite consumption is also runtime-free (just `repo2rlenv reward`).

    - `provider="harbor"` (full pipelines like `pr_runtime`) — at gen time we
      shell out to `harbor` with `harbor_provider` selecting the underlying
      backend (Local Docker / Modal / Daytona / E2B / Runloop). Consumers
      run tasks via `harbor run -d <dataset> -e <provider> ...` directly.
    """

    provider: Literal["none", "harbor"] = "none"
    harbor_provider: Literal["local", "modal", "daytona", "e2b", "runloop"] = "local"
    concurrency: int = 10
    network: Literal["open", "build_open_run_restricted", "none"] = "build_open_run_restricted"
    gpu: GPUSpec | None = None
    image_registry: str | None = None
    timeout_sec: int = 600


class GPUSpec(BaseModel):
    """GPU request, lowered to the Harbor provider's native config.

    Only meaningful for full sandbox-required pipelines (`pr_runtime` etc.)
    on ML repositories whose test suites require CUDA. Lite pipelines never
    use this. Provider support varies:
      - Modal:   rich (a10g, a100, h100, ...)
      - Daytona: yes
      - Runloop: yes (limited)
      - E2B:     limited (CPU primarily)
      - Local:   only if host has GPU + nvidia-container-runtime
    """

    count: int = 1
    kind: Literal["any", "a10g", "a100", "h100", "l4", "t4"] = "any"


class OutputSpec(BaseModel):
    destination: str
    org: str
    dataset_name: str
    visibility: Literal["public", "private"] = "public"


class AuthSpec(BaseModel):
    github_token_env: str = "GITHUB_TOKEN"
    use_gh_cli: bool = True
    hf_token_env: str = "HF_TOKEN"
    use_hf_cli: bool = True
    build_secrets_env: dict[str, str] = Field(default_factory=dict)


class BootstrapSpec(BaseModel):
    """Bootstrap phase config — only used by sandbox-required pipelines.

    Lite pipelines (text-only) ignore this entirely. For full pipelines, the
    bootstrap phase builds a Docker image where the repo cleanly compiles
    and tests can run, then caches it by content hash so subsequent
    generation runs reuse the same image.

    See docs/BOOTSTRAP.md for the design.
    """

    enabled: bool = True
    max_iterations: int = 20
    max_seconds: int = 1800  # 30-minute timeout per bootstrap
    base_image: str | None = None  # override per-language default
    user_dockerfile: Path | None = None  # bypass agent iteration entirely
    cache_dir: Path = Field(
        default_factory=lambda: Path(os.environ.get("R2E_CACHE_DIR", "./workspace/bootstrap"))
    )
    image_registry: str | None = None  # e.g. "ghcr.io/myorg"; None ⇒ keep local
    max_llm_spend_usd: float | None = 5.0
    platform: Literal["linux/amd64", "linux/arm64"] = "linux/amd64"
    languages_hint: list[str] | None = None  # override auto-detection


class PipelineSpec(BaseModel):
    name: PipelineName
    recipe: str = "native"
    options: dict[str, Any] = Field(default_factory=dict)


class RecipeExecutionSpec(BaseModel):
    """An owned recipe's explicit, recoverable remote execution context."""

    model_config = ConfigDict(extra="forbid")
    worker_receipt: Path
    runtime_wheel: Path
    campaign_dir: Path
    run_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,60}$")
    timeout_sec: int = Field(default=1800, ge=60, le=14400)
    resume: bool = False


class GenerationInput(BaseModel):
    spec_version: Literal["0.1.0"] = "0.1.0"
    repo: RepoSpec | None = None
    source: SourceSpec | None = None
    pipeline: PipelineSpec
    llm: LLMSpec | None = None
    output: OutputSpec
    qa: QASpec = Field(default_factory=QASpec)
    sandbox: SandboxSpec = Field(default_factory=SandboxSpec)
    bootstrap: BootstrapSpec = Field(default_factory=BootstrapSpec)
    auth: AuthSpec = Field(default_factory=AuthSpec)
    execution: RecipeExecutionSpec | None = None

    @model_validator(mode="after")
    def normalize_source(self) -> GenerationInput:
        if self.source is None:
            if self.repo is None:
                raise ValueError("Provide a repository or a typed source")
            self.source = RepositorySource(repo=self.repo)
        elif isinstance(self.source, RepositorySource):
            if self.repo is not None and self.repo != self.source.repo:
                raise ValueError("repo and source.repo describe different inputs")
            self.repo = self.source.repo
        elif self.repo is not None:
            raise ValueError("A non-repository source cannot also specify repo")
        if self.pipeline.recipe == "native" and self.repo is None:
            raise ValueError(
                "Native pipelines require a repository; select an owned recipe for this source"
            )
        return self

    @property
    def source_label(self) -> str:
        if self.repo is not None:
            return self.repo.url
        if isinstance(self.source, PRSource):
            return (
                self.source.urls[0]
                if len(self.source.urls) == 1
                else f"{len(self.source.urls)} PRs"
            )
        return str(self.source.path)


LLMSpec.model_rebuild()
SandboxSpec.model_rebuild()
BootstrapSpec.model_rebuild()
