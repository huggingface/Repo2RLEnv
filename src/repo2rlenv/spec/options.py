"""Per-pipeline options (the "kwargs" each pipeline accepts)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _BaseOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PRRuntimeOptions(_BaseOptions):
    """Sandbox-verified PR mining: clones, applies diff, runs tests in bootstrap image.

    The pipeline runs each candidate PR's tests inside the bootstrap container
    twice — once with only `test_patch` applied (to capture which tests fail
    pre-fix), and once with both `test_patch` and the gold `patch` applied
    (to confirm which now pass). Tests that transition fail→pass become the
    `FAIL_TO_PASS` oracle; tests that pass both times become `PASS_TO_PASS`
    regression guards. See docs/pipelines/pr_runtime.md.
    """

    # --- Mining (mirrors PRDiffOptions where overlap exists) ---
    limit: int = 50
    since: date | None = None
    until: date | None = None
    state: Literal["merged"] = "merged"
    skip_drafts: bool = True
    require_linked_issue: bool = True
    languages: list[str] = ["python"]

    # --- Validation ---
    require_fail_to_pass: bool = True  # skip PRs whose F2P set is empty after validation
    min_fail_to_pass: int = 1
    validation_timeout_sec: int = 600  # per-PR cap on the two test runs
    skip_validation: bool = False  # emit candidates without F2P/P2P (debug / fast iteration)

    # --- Quality (SWE-bench Lite-style sampling) ---
    lite_filter: bool = False
    max_source_files_per_pr: int = 50  # PRs touching >N source files are excluded
    min_problem_statement_words: int = 0  # Lite ≈ 40

    # --- Structural filters (cheap, applied before validation) ---
    require_new_test_funcs: bool = True  # test_patch must add ≥1 new test func/class
    skip_ci_only: bool = True  # auto-skip when source patch is 100% under .github/


class PRDiffOptions(_BaseOptions):
    """SWE-RL-style: text-only PR mining, no execution, no Docker."""

    limit: int = 50
    since: date | None = None
    until: date | None = None
    state: Literal["merged", "all"] = "merged"
    context_window_loc: int = 200
    diff_format: Literal["unified", "search_replace"] = "unified"
    max_files_per_pr: int = 5
    skip_drafts: bool = True


class PRStreamOptions(PRRuntimeOptions):
    """Continuous (SWE-bench-Live-style) PR mining.

    `pr_stream` is `pr_runtime` + state. Re-running the same command later
    picks up where the previous run left off — only NEW PRs (those merged
    after the watermark) are processed. The watermark advances after each
    successful run.

    Inherits every PRRuntimeOptions field. Adds:
      cutoff_date         — earliest merged_at to mine; combines with the
                            watermark via `since = max(cutoff_date, watermark)`.
                            Use this to scope mining to post-model-cutoff
                            PRs (contamination-resistant).
      state_dir           — where the watermark JSON lives. Defaults to
                            `./envs/streams/` so it sits alongside bootstrap
                            cache without polluting the dataset out_dir.
    """

    cutoff_date: date | None = None
    state_dir: str = "./envs"


class CommitRuntimeOptions(_BaseOptions):
    """Commit-level mining (R2E-Gym SWE-GEN style).

    Walks `git log` instead of `gh pr list`. Same validation harness as
    `pr_runtime` once we have a (patch, test_patch, base_commit) tuple.
    Commits are noisier than PRs — we filter aggressively at the file +
    message level before running the (expensive) validation harness.
    """

    # --- Mining ---
    limit: int = 50
    since: date | None = None
    until: date | None = None
    branch: str = "HEAD"
    clone_depth: int = 200  # deeper than bootstrap's depth=1 so git log can walk

    # --- Filters (cheap, applied before validation) ---
    skip_merge_commits: bool = True
    min_message_words: int = 5  # drop "wip", "fmt", "typo" etc.
    max_source_files_per_commit: int = 10
    exclude_authors: list[str] = []  # e.g. ["dependabot[bot]@users.noreply.github.com"]
    require_new_test_funcs: bool = True  # test_patch must add ≥1 new test func
    skip_ci_only: bool = True

    # --- Validation (mirrors PRRuntimeOptions) ---
    require_fail_to_pass: bool = True
    min_fail_to_pass: int = 1
    validation_timeout_sec: int = 600
    skip_validation: bool = False

    # --- Instruction synthesis ---
    synthesize_with_llm: bool = False  # if False, use raw commit subject + body
    min_problem_statement_words: int = 0


class MutationBugsOptions(_BaseOptions):
    """SWE-smith-style synthetic bug injection.

    Picks Python source files in the target repo, applies an AST mutation
    operator (flip_comparison / off_by_one / swap_arithmetic / ...), runs
    the existing test suite, and accepts the mutation if it breaks between
    `min_tests_broken` and `max_tests_broken` tests.

    The "fix" the agent must produce is the inverse mutation; the oracle
    is the original (pre-mutation) source. See docs/pipelines/mutation_bugs.md.
    """

    # --- Discovery ---
    limit: int = 50
    file_glob: str = "**/*.py"
    exclude_glob: list[str] = [
        "tests/**",
        "test_**",
        "**/test_*.py",
        "**/*_test.py",
        "**/conftest.py",
        "**/setup.py",
        "docs/**",
        "examples/**",
        "**/__init__.py",  # mutating __init__ tends to break imports catastrophically
    ]

    # --- Operators ---
    operators: list[str] | None = None  # None ⇒ use every default operator
    seed: int | None = None  # RNG seed for reproducibility (None ⇒ time-based)
    max_attempts_per_file: int = 5  # give up on a file if it refuses to mutate productively

    # --- Mutation filter ---
    min_tests_broken: int = 1
    max_tests_broken: int = 5
    validation_timeout_sec: int = 300
    skip_validation: bool = False  # emit candidates raw (debug / fast iteration)
    # If set, restrict pytest to this path (or space-separated list of paths).
    # Lets fast iteration scope to one test file (e.g. `tests/test_basic.py`)
    # instead of running the whole suite per mutation candidate. The emitted
    # task's verifier still uses the targeted file list derived from the
    # specific broken tests, so this only affects the GENERATION-TIME scan.
    test_target: str | None = None

    # --- LLM ---
    llm_temperature: float = 0.7
    max_llm_tokens: int = 1024


class CodeInstructOptions(_BaseOptions):
    """Magicoder-OSS-Instruct-style, anchored to a target repo + verified by execution.

    Samples a seed snippet from the repo, asks the LLM for a self-contained
    coding task (problem statement + pytest test + oracle solution), then
    verifies in the bootstrap container: the test must FAIL on HEAD and PASS
    once the oracle solution is applied. Failures are skipped.

    See docs/pipelines/code_instruct.md.
    """

    # --- Sampling ---
    limit: int = 50
    seed_min_loc: int = 30
    seed_max_loc: int = 200
    file_glob: str = "**/*.py"
    exclude_glob: list[str] = [
        "tests/**",
        "test_**",
        "**/test_*.py",
        "**/*_test.py",
        "docs/**",
        "examples/**",
        "**/__init__.py",
    ]
    seed: int | None = None
    max_attempts_per_seed: int = 1

    # --- LLM ---
    llm_temperature: float = 0.7
    max_llm_tokens: int = 2048

    # --- Verification ---
    require_test_fails_without_oracle: bool = True
    require_test_passes_with_oracle: bool = True
    validation_timeout_sec: int = 180
    skip_validation: bool = False

    # --- Decontamination ---
    skip_decontamination: bool = False


OPTIONS_REGISTRY: dict[str, type[_BaseOptions]] = {
    "pr_runtime": PRRuntimeOptions,
    "pr_diff": PRDiffOptions,
    "pr_stream": PRStreamOptions,
    "commit_runtime": CommitRuntimeOptions,
    "mutation_bugs": MutationBugsOptions,
    "code_instruct": CodeInstructOptions,
}


def parse_options(pipeline_name: str, raw: dict) -> _BaseOptions:
    cls = OPTIONS_REGISTRY.get(pipeline_name)
    if cls is None:
        raise ValueError(
            f"pipeline {pipeline_name!r} has no Options registered "
            f"(known: {sorted(OPTIONS_REGISTRY)})"
        )
    return cls.model_validate(raw)
