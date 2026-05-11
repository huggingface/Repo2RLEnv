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
    require_fail_to_pass: bool = True       # skip PRs whose F2P set is empty after validation
    min_fail_to_pass: int = 1
    validation_timeout_sec: int = 600       # per-PR cap on the two test runs
    skip_validation: bool = False           # emit candidates without F2P/P2P (debug / fast iteration)

    # --- Quality (SWE-bench Lite-style sampling) ---
    lite_filter: bool = False
    max_source_files_per_pr: int = 50       # PRs touching >N source files are excluded
    min_problem_statement_words: int = 0    # Lite ≈ 40

    # --- Structural filters (cheap, applied before validation) ---
    require_new_test_funcs: bool = True     # test_patch must add ≥1 new test func/class
    skip_ci_only: bool = True               # auto-skip when source patch is 100% under .github/


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


OPTIONS_REGISTRY: dict[str, type[_BaseOptions]] = {
    "pr_runtime": PRRuntimeOptions,
    "pr_diff": PRDiffOptions,
}


def parse_options(pipeline_name: str, raw: dict) -> _BaseOptions:
    cls = OPTIONS_REGISTRY.get(pipeline_name)
    if cls is None:
        raise ValueError(
            f"pipeline {pipeline_name!r} has no Options registered "
            f"(known: {sorted(OPTIONS_REGISTRY)})"
        )
    return cls.model_validate(raw)
