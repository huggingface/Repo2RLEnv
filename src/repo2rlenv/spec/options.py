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
    """SWE-RL-style PR mining with a Harbor-runnable diff-similarity verifier.

    Each emitted task includes an environment/Dockerfile (python:3.12-slim +
    git + the repo checked out at base_commit + the oracle diff baked in)
    and a tests/test.sh that captures the agent's edits via `git diff` and
    scores them against the oracle using SWE-RL-style sequence similarity
    (mirrors `repo2rlenv.reward.calculate_diff_similarity_reward`). The
    Dockerfile is intentionally minimal — no LLM bootstrap — so cells stay
    cheap.
    """

    limit: int = 50
    since: date | None = None
    until: date | None = None
    state: Literal["merged", "all"] = "merged"
    context_window_loc: int = 200
    diff_format: Literal["unified", "search_replace"] = "unified"
    max_files_per_pr: int = 5
    skip_drafts: bool = True
    # Emit environment/Dockerfile + tests/test.sh so the task is a fully
    # Harbor-runnable env. Default on. Set False to fall back to the
    # v0.8.1 text-only output (just instruction.md + solution/patch.diff)
    # for training pipelines that compute the reward externally.
    emit_harbor_env: bool = True
    # Minimum number of +/- lines in the oracle diff to accept as a task.
    # Below this is too trivial to be a meaningful RL signal — typically
    # a one-character typo fix or a doc tweak.
    min_loc_changed: int = 3


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
    # Cap PASS_TO_PASS regression set: whole-suite P2P (100s of tests) inflates
    # flakiness + runtime. Keep a bounded, still-meaningful guard. 0 = no cap.
    max_pass_to_pass: int = 50

    # --- Instruction synthesis ---
    # Default ON: rewrite the commit/issue into a clean, leak-free, symptom-
    # focused problem statement. Raw commit messages either leak the solution
    # (changelog bullets) or are too thin (title-only) — both hurt solvability.
    synthesize_with_llm: bool = True
    # Low floor: with synthesis ON the LLM expands terse commits, so we only
    # need *some* signal. 20 starved terse-commit repos (Rust crates, many Go);
    # 8 keeps near-empty/title-only out while letting synthesis do the rest.
    min_problem_statement_words: int = 8
    llm_temperature: float = 0.3
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
    # Repo-anchoring, symbol-collision, and test-strength gates reject a lot
    # of the LLM's first drafts; give it a few tries per seed before moving on.
    max_attempts_per_seed: int = 3

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


class CVEPatchesOptions(_BaseOptions):
    """Map OSV vulnerability records to fixing commits in the target repo.

    For each vuln returned by OSV's `/v1/query`, find a `references[]` URL
    pointing at `github.com/<owner>/<repo>/commit/<sha>`, fetch that
    commit's diff, split into source/test patches, and emit a Harbor task
    whose verifier mirrors `commit_runtime` (F2P/P2P validation when a
    test_patch is present; emission-only otherwise).

    See docs/pipelines/cve_patches.md.
    """

    # --- OSV discovery ---
    osv_ecosystem: str | None = None  # "PyPI" / "npm" / "crates.io" / ... (None ⇒ auto-guess)
    osv_package: str | None = None  # package name (None ⇒ use repo name)
    min_severity: Literal["low", "medium", "moderate", "high", "critical"] = "low"

    # --- Output cap ---
    limit: int = 50

    # --- Validation (mirrors PRRuntimeOptions) ---
    # Default ON: CVE fixes rarely ship a regression test, so an LLM synthesizes
    # a PoC test that must FAIL on the pre-fix code and PASS on the post-fix code
    # (real F2P oracle). Without this, no-test CVEs emit a 0-reward dead env.
    synthesize_poc_test: bool = True
    poc_agent: bool = True  # agentic synth (shell in the sandbox) vs one-shot prompt
    poc_agent_max_spend_usd: float = 1.5  # per-CVE budget for the agentic synthesizer
    poc_max_attempts: int = 2  # one-shot mode: retry a bad PoC generation this many times
    require_fail_to_pass: bool = True  # with PoC synthesis we demand a real oracle
    min_fail_to_pass: int = 1
    max_pass_to_pass: int = 50  # cap regression set (bounds flaky-reward + runtime)
    validation_timeout_sec: int = 600
    skip_validation: bool = False
    llm_temperature: float = 0.3
    max_llm_tokens: int = 4096  # PoC test files can be long; raise via --pipeline-opt if needed

    # --- Structural filters ---
    require_new_test_funcs: bool = False  # security commits often DON'T add new tests
    max_source_files_per_fix: int = 50


class EquivalenceTestsOptions(_BaseOptions):
    """R2E-style function-level equivalence-test synthesis.

    Extracts module-level Python functions from the target repo, asks the
    LLM to write a pytest test that calls both `<name>` (candidate, stubbed
    in env) and `reference_<name>` (frozen oracle) with crafted inputs and
    asserts outputs match. Verifies in-sandbox: the test must FAIL when
    `<name>` is stubbed and PASS when `<name>` is the original.

    See docs/pipelines/equivalence_tests.md.
    """

    # --- Discovery ---
    limit: int = 50
    min_loc: int = 5  # min lines in function body
    max_loc: int = 60  # max lines in function body
    file_glob: str = "**/*.py"
    exclude_glob: list[str] = [
        "tests/**",
        "test_**",
        "**/test_*.py",
        "**/*_test.py",
        "**/conftest.py",
        "docs/**",
        "examples/**",
        "**/__init__.py",
        "**/setup.py",
    ]
    seed: int | None = None
    max_attempts_per_function: int = 1

    # --- LLM ---
    llm_temperature: float = 0.5  # lower than code_instruct — we want stable tests
    max_llm_tokens: int = 1500

    # --- Verification ---
    require_test_fails_with_stub: bool = True
    require_test_passes_with_oracle: bool = True
    validation_timeout_sec: int = 90
    skip_validation: bool = False


OPTIONS_REGISTRY: dict[str, type[_BaseOptions]] = {
    "pr_runtime": PRRuntimeOptions,
    "pr_diff": PRDiffOptions,
    "commit_runtime": CommitRuntimeOptions,
    "code_instruct": CodeInstructOptions,
    "equivalence_tests": EquivalenceTestsOptions,
    "cve_patches": CVEPatchesOptions,
}


def parse_options(pipeline_name: str, raw: dict) -> _BaseOptions:
    cls = OPTIONS_REGISTRY.get(pipeline_name)
    if cls is None:
        raise ValueError(
            f"pipeline {pipeline_name!r} has no Options registered "
            f"(known: {sorted(OPTIONS_REGISTRY)})"
        )
    return cls.model_validate(raw)
