"""Sandbox-verified PR mining (SWE-bench-style).

For each merged PR within scope:
  1. Pull metadata + unified diff via `gh pr list` / `gh pr diff`
  2. Split the diff into `patch` (source files) and `test_patch` (test files)
     using the same keyword-on-path heuristic SWE-bench uses
  3. If validation enabled: run the bootstrap container twice (once with
     test_patch only, once with both patches) to compute FAIL_TO_PASS and
     PASS_TO_PASS sets — the verified oracle
  4. Emit a Harbor task with environment/Dockerfile (FROM <bootstrap_image>),
     tests/test.sh (the eval script), and solution/patch.diff (gold patch)

Unlike `pr_diff`, this pipeline requires a working Docker image from the
bootstrap phase. `cmd_generate` triggers `ensure_bootstrap()` automatically
when `requires_bootstrap=True`.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
This pipeline mirrors the data-collection + validation approach of:

  SWE-bench: Can Language Models Resolve Real-world Github Issues?
  (Jimenez et al., ICLR '24, arXiv:2310.06770)
  https://github.com/SWE-bench/SWE-bench        (MIT)

  SWE-bench-Live: A Live Benchmark for Issue Resolving
  (Zhang et al., NIPS '25, arXiv:2505.23419)
  https://github.com/microsoft/SWE-bench-Live   (MIT)

We adapt the patch-split heuristic (collect/utils.py:extract_patches),
the eval-script structure (harness/test_spec/utils.py:make_eval_script_list_common),
and the F2P/P2P grading semantics (harness/grading.py). No code is copied;
we don't depend on the `swebench` PyPI package.

Released under Apache-2.0 along with the rest of Repo2RLEnv.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_github_token
from repo2rlenv.bootstrap.spec import BootstrapResult
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.github import (
    GitHubError,
    PullRequestSummary,
    fetch_pr_diff,
    list_merged_prs,
)
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import PRRuntimeOptions

logger = logging.getLogger(__name__)


_CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#\d+\b", re.IGNORECASE)

# Path-component classifier for "is this a test file?".
#
# SWE-bench's heuristic is a substring match on the full path — which over-fires:
# `docs/testing.md`, `src/click/testing.py`, etc. all become "test files".
# We instead match on PATH COMPONENTS (split by /) and explicitly exclude
# documentation paths.
_TEST_DIR_NAMES = {"test", "tests", "testing", "e2e", "__tests__"}
_DOC_PREFIX_DIRS = {"docs", "doc", "documentation", "examples", "example"}


def _path_is_test(path: str) -> bool:
    """True if the file is a real test file, false for docs / src files
    that merely contain a test keyword in their path.

    Rules:
      1. Files under a documentation root (`docs/`, `examples/`, ...) are
         NEVER test files, even if their name contains "test".
      2. Files inside a test directory component (any path part in
         `_TEST_DIR_NAMES`) are test files.
      3. Files with a pytest-style basename (`test_*.py`, `*_test.py`,
         `*_test.go`) are test files.
    """
    if not path:
        return False
    parts = [p.lower() for p in path.split("/") if p]
    if not parts:
        return False
    # Rule 1: skip anything under a docs root
    if parts[0] in _DOC_PREFIX_DIRS:
        return False
    # Rule 2: any directory component is a known test dir
    # (excluding the last component, which is the file name)
    for component in parts[:-1]:
        if component in _TEST_DIR_NAMES:
            return True
    # Rule 3: filename-level test markers
    basename = parts[-1]
    return (
        (basename.startswith("test_") and basename.endswith((".py", ".js", ".ts")))
        or basename.endswith(("_test.py", "_test.go", ".test.ts", ".test.js"))
        or basename.endswith((".spec.ts", ".spec.js"))
    )


# Match `diff --git a/<path> b/<path>` block boundaries to split a unified diff
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def split_patch_and_test_patch(unified_diff: str) -> tuple[str, str]:
    """Split a PR's unified diff into (source patch, test patch).

    SWE-bench rule: a file hunk goes into `test_patch` iff its path contains
    one of `test/tests/e2e/testing`; everything else goes into `patch`.

    We walk the diff by `diff --git` markers (one per file in the PR) so we
    keep each file's hunks intact.
    """
    if not unified_diff.strip():
        return "", ""

    # Find each "diff --git a/X b/Y" header and the byte offset where its block starts
    matches = list(_DIFF_HEADER_RE.finditer(unified_diff))
    if not matches:
        # Empty / malformed — return whole thing as patch
        return unified_diff, ""

    patch_parts: list[str] = []
    test_parts: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(unified_diff)
        block = unified_diff[start:end]
        path_a = m.group(1)
        path_b = m.group(2)
        # If EITHER path looks like a test file (covers renames, new files), it's a test
        if _path_is_test(path_a) or _path_is_test(path_b):
            test_parts.append(block)
        else:
            patch_parts.append(block)

    return "".join(patch_parts), "".join(test_parts)


def _build_instruction(pr: PullRequestSummary) -> str:
    """Strip 'Closes #N' style boilerplate from the PR description."""
    body = (pr.body or "").strip()
    body = _CLOSES_RE.sub("", body).strip()
    if not body:
        body = "(no description provided in source PR)"
    return (
        f"# Issue\n\n"
        f"**Title:** {pr.title}\n\n"
        f"## Description\n\n"
        f"{body}\n\n"
        f"## Task\n\n"
        f"Modify the repository so that the issue described above is resolved. "
        f"The task's test suite verifies your patch by applying it on top of "
        f"the base commit `{pr.base_sha[:12]}` and running the modified tests."
    )


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def build_environment_dockerfile(bootstrap_image: str, base_commit: str) -> str:
    """Build the per-task environment/Dockerfile.

    The bootstrap image has the repo at the bootstrap-time HEAD (whatever
    ref was used during `repo2rlenv bootstrap`). Each PR has its own
    `base_commit` — usually NOT bootstrap HEAD. If Harbor builds the agent
    image from the bootstrap image as-is, the agent + verifier see
    HEAD-state source files, but the gold patch (and any model patch) was
    written against `base_commit`-state files. Patches would fail to apply
    against the wrong line context.

    Fix: at build time, fetch `base_commit` (in case shallow clone doesn't
    have it) and reset the working tree to it. This makes Harbor's
    "apply model patch then run test.sh" flow correct.

    `bootstrap_image` should be the tag (e.g. `local/r2e-bootstrap/foo:abc`)
    for local-only bootstraps and the registry-qualified digest
    (`ghcr.io/owner/foo@sha256:...`) for pushed images. Docker BuildKit's
    `FROM <name>@sha256:...` syntax tries to fetch from a registry — local
    digest references don't work. The caller (`_build_task`) picks the
    right form based on `BootstrapResult.pushed_to_registry`.
    """
    return (
        f"# Auto-generated by Repo2RLEnv pr_runtime\n"
        f"FROM {bootstrap_image}\n"
        f"WORKDIR /workspace\n"
        f"# Position the working tree at the PR's base commit so subsequent\n"
        f"# model-patch applications align with the line context the patch\n"
        f"# was authored against. The fetch is a no-op if the commit is\n"
        f"# already in the shallow clone.\n"
        f"RUN git config --global --add safe.directory /workspace \\\n"
        f"    && git fetch --depth 1 origin {base_commit} 2>/dev/null \\\n"
        f"       || git fetch --unshallow origin 2>/dev/null || true\n"
        f"RUN git reset --hard {base_commit} && git clean -fdx -e .venv -e venv -e __pycache__\n"
    )


def _path_prelude_for_language(language: str | None) -> str:
    """Shell snippet that prepends common toolchain dirs to $PATH.

    The bootstrap agent often installs language toolchains (Go, Rust,
    Node) into well-known paths (`/usr/local/go/bin`, `~/.cargo/bin`,
    nvm dirs) but doesn't always persist a corresponding `export PATH`
    to a shell init file. When Harbor's verifier runs `bash test.sh` in
    a non-interactive shell, those binaries vanish from PATH → exit 127
    on `go test` / `cargo test` / `node` → false-negative reward 0.

    The fix at emission time: prepend the known install locations for
    the bootstrap-detected language so the verifier shell always finds
    the runner binary. Missing dirs are no-ops; the cost is one extra
    line in test.sh.
    """
    extras = {
        "go": ["/usr/local/go/bin", "$HOME/go/bin"],
        "rust": ["$HOME/.cargo/bin"],
        "node": ["/usr/local/lib/node_modules/.bin", "$HOME/.nvm/versions/node/*/bin"],
        "java": ["/usr/lib/jvm/default-java/bin"],
    }
    dirs = extras.get((language or "").lower(), [])
    if not dirs:
        return ""
    joined = ":".join(dirs)
    return f'export PATH="{joined}:$PATH"\n'


def build_eval_script(
    base_commit: str,
    test_patch: str,
    test_cmds: list[str],
    *,
    language: str | None = None,
) -> str:
    """Build the `tests/test.sh` content that Harbor runs after the model patch.

    Adapted from SWE-bench's `harness/test_spec/utils.py:make_eval_script_list_common`.
    The flow:
      1. cd /workspace + mark safe.directory (for non-root git operations)
      2. Prepend known toolchain paths for the detected language (compensates
         for bootstrap agents that install Go/Rust/Node outside /usr/bin
         without exporting PATH in any persisted shell init file)
      3. Reset test files to base_commit (so re-running stays clean)
      4. Apply the test_patch (via heredoc + git apply --reject)
      5. Run test_cmds bracketed with START_TEST_OUTPUT / END_TEST_OUTPUT markers
         so the log parser knows where tests started
      6. Write the reward to /logs/verifier/reward.txt (Harbor's verifier
         reads this; exit code alone isn't enough — see Verifier._parse_reward_text)
      7. Reset test files again on the way out

    The model's predicted patch is applied by Harbor *before* this script runs.
    """
    test_files = _files_in_patch(test_patch)
    heredoc = "EOF_R2E_TEST_PATCH"
    reset = (
        f"git checkout {base_commit} -- {' '.join(test_files)}"
        if test_files
        else "echo 'no test files to reset'"
    )
    apply = f"git apply --verbose --reject - <<'{heredoc}'\n{test_patch}\n{heredoc}"
    test_block = " && ".join(test_cmds) if test_cmds else "echo 'no test_cmds configured'"
    path_prelude = _path_prelude_for_language(language)
    return (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        f"{path_prelude}"  # may be empty
        "cd /workspace\n"
        "git config --global --add safe.directory /workspace\n"
        "mkdir -p /logs/verifier\n"
        f"{reset} || true\n"  # tolerate test files that didn't exist at base
        f"{apply}\n"
        ": 'START_TEST_OUTPUT'\n"
        f"{test_block}\n"
        "TEST_EXIT_CODE=$?\n"
        ": 'END_TEST_OUTPUT'\n"
        # Harbor verifier reads /logs/verifier/reward.txt — write 1.0 if all
        # tests passed, else 0.0. Exit-code-based pass/fail alone doesn't
        # populate the reward file the verifier expects.
        '[ "$TEST_EXIT_CODE" -eq 0 ] && echo "1.0" > /logs/verifier/reward.txt '
        '|| echo "0.0" > /logs/verifier/reward.txt\n'
        f"{reset} || true\n"  # cleanup; failure here doesn't change verdict
        "exit $TEST_EXIT_CODE\n"
    )


def _files_in_patch(unified_diff: str) -> list[str]:
    """Extract the unique 'b/' file paths touched by a unified diff."""
    if not unified_diff.strip():
        return []
    seen: list[str] = []
    for m in _DIFF_HEADER_RE.finditer(unified_diff):
        b = m.group(2)
        if b not in seen:
            seen.append(b)
    return seen


# Lines in a test_patch that introduce a new test function/class.
# Python: `+def test_foo(...)`, `+    def test_bar(...)`, `+class TestX`
# JS/TS:  `+it(`, `+test(`, `+describe(` (not currently filtered)
# Go:     `+func TestFoo(`
_NEW_TEST_FUNC_RE = re.compile(
    r"^\+\s*(?:def\s+test_\w+|class\s+\w*[Tt]est\w*|func\s+Test\w+|it\s*\(|test\s*\(|describe\s*\()",
)


def _count_new_test_funcs(test_patch: str) -> int:
    """Count new test-function definitions added in a unified diff.

    Used to filter out PRs whose test_patch is comment-only or docstring-only
    (cosmetic changes that can't produce a FAIL_TO_PASS oracle).
    """
    if not test_patch.strip():
        return 0
    return sum(1 for line in test_patch.splitlines() if _NEW_TEST_FUNC_RE.match(line))


def normalize_test_cmds_for_runtime(test_cmds: list[str]) -> list[str]:
    """Adapt bootstrap-recorded test commands for actual per-PR execution.

    Bootstrap prefers fast/tolerant commands (e.g. `pytest --collect-only`)
    so it can declare success without running every test. For pr_runtime,
    we need commands that *run* tests and emit per-test pass/fail lines
    that our parsers can read.

    Transforms (per runner):
      pytest:
        - Drop `--collect-only` / `--co` so pytest actually runs tests
        - Add `-v` if no verbosity flag is present
      go test:
        - Add `-v` if missing (default `go test` doesn't print --- PASS lines)
      cargo test:
        - Default output is already parseable; no transform needed
      jest / npm test:
        - Add `--verbose` if not present, so per-test ✓/✕ lines are emitted
        - Some configs swallow stdout via `--silent`; we strip that
    """
    out: list[str] = []
    for cmd in test_cmds:
        cleaned = cmd

        # --- pytest ---
        if re.search(r"\bpytest\b", cleaned):
            cleaned = re.sub(r"\s+--collect-only\b", "", cleaned)
            cleaned = re.sub(r"\s+--co\b", "", cleaned)  # pytest's short form
            if not re.search(r"\s-v\b|\s--verbose\b|-vv\b", cleaned):
                cleaned = cleaned.rstrip() + " -v"

        # --- go test ---
        elif re.search(r"\bgo\s+test\b", cleaned):
            if not re.search(r"\s-v\b", cleaned):
                # Insert -v right after `go test`; positional args go after
                cleaned = re.sub(r"\bgo\s+test\b", "go test -v", cleaned, count=1)

        # --- cargo test ---
        elif re.search(r"\bcargo\s+test\b", cleaned):
            # `cargo test` already prints `test NAME ... ok/FAILED/ignored`
            # by default — no transformation needed. If a user passed
            # `-q`, the per-test lines disappear; strip it.
            cleaned = re.sub(r"\s+(?:-q|--quiet)\b", "", cleaned)

        # --- jest / npm test / yarn test / pnpm test ---
        elif re.search(r"\b(?:jest|mocha|vitest|npm\s+test|yarn\s+test|pnpm\s+test)\b", cleaned):
            cleaned = re.sub(r"\s+--silent\b", "", cleaned)
            # Add --verbose if the cmd is the runner itself (skip wrappers
            # where flags need to go after `--`)
            if re.search(r"\b(?:jest|mocha|vitest)\b", cleaned) and not re.search(
                r"\s--verbose\b|\s--reporter\b", cleaned
            ):
                cleaned = cleaned.rstrip() + " --verbose"

        out.append(cleaned.strip())
    return out


# Pytest-style file extensions we know how to target. Anything else triggers
# the fallback to "run the whole suite".
_PYTEST_TARGETABLE_EXT = (".py",)
_JEST_TARGETABLE_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


def _go_packages_from_test_files(test_files: list[str]) -> list[str]:
    """Map `pkg/foo/bar_test.go` → `./pkg/foo` for Go's package-path CLI."""
    pkgs: list[str] = []
    for f in test_files:
        if not f.endswith("_test.go"):
            continue
        # Directory of the test file, prefixed with ./ for `go test` package syntax
        parts = f.rsplit("/", 1)
        pkg = "./" + parts[0] if len(parts) == 2 else "./"
        if pkg not in pkgs:
            pkgs.append(pkg)
    return pkgs


def targeted_test_cmds_for_pr(test_cmds: list[str], test_files: list[str]) -> list[str]:
    """Limit the test invocation to the file paths the PR's test_patch touches.

    Running the whole suite on every PR is 10-50× slower than running only
    the files the PR cares about. SWE-bench-Live's harness does the same.

    Per-runner rules:
      pytest:  append the changed .py test files as positional args
      jest:    append the changed .js/.ts test files as positional args
      go test: replace `./...` (or trailing nothing) with the package
               directories containing changed `*_test.go` files
      cargo:   no targeting — Rust's filter is name-substring, not file
               (we'd need to introspect test names; whole-suite is fine)

    Skips if the cmd already has a positional path arg.
    """
    if not test_files:
        return test_cmds

    py_files = [f for f in test_files if f.endswith(_PYTEST_TARGETABLE_EXT)]
    js_files = [f for f in test_files if f.endswith(_JEST_TARGETABLE_EXT)]
    go_pkgs = _go_packages_from_test_files(test_files)

    out: list[str] = []
    for cmd in test_cmds:
        # --- pytest ---
        if re.search(r"\bpytest\b", cmd) and py_files:
            tokens = cmd.split()
            pytest_idx = next(
                (i for i, t in enumerate(tokens) if t == "pytest" or t.endswith("/pytest")),
                -1,
            )
            if pytest_idx >= 0:
                tail = tokens[pytest_idx + 1 :]
                has_path_arg = any(
                    not t.startswith("-") and (t.endswith(".py") or "/" in t) for t in tail
                )
                if not has_path_arg:
                    cmd = cmd.rstrip() + " " + " ".join(py_files)

        # --- go test ---
        elif re.search(r"\bgo\s+test\b", cmd) and go_pkgs:
            # Replace `./...` with the targeted packages; if neither is present,
            # append the packages
            if "./..." in cmd:
                cmd = cmd.replace("./...", " ".join(go_pkgs))
            elif not re.search(r"\b\./\S+\b", cmd):
                cmd = cmd.rstrip() + " " + " ".join(go_pkgs)

        # --- jest / npx jest / mocha / vitest ---
        elif re.search(r"\b(?:jest|mocha|vitest)\b", cmd) and js_files:
            tokens = cmd.split()
            # If a positional file path is already present, don't double-up
            has_path = any(
                not t.startswith("-") and (t.endswith(_JEST_TARGETABLE_EXT) or "/" in t)
                for t in tokens[1:]
            )
            if not has_path:
                cmd = cmd.rstrip() + " " + " ".join(js_files)

        out.append(cmd)
    return out


class PRRuntimePipeline:
    """Sandbox-verified PR mining. Implements the `Pipeline` Protocol."""

    name: ClassVar[PipelineName] = PipelineName.PR_RUNTIME
    requires_bootstrap: ClassVar[bool] = True

    def __init__(
        self,
        input: GenerationInput,
        options: PRRuntimeOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "pr_runtime requires a BootstrapResult (set requires_bootstrap=True "
                "and let cmd_generate trigger it, or pass one explicitly)"
            )
        self.input = input
        self.options = options
        self.bootstrap = bootstrap
        self._progress_cb = None

    def set_progress_callback(self, cb) -> None:
        self._progress_cb = cb

    def _emit_progress(self, name: str, outcome: str, reason: str = "") -> None:
        if self._progress_cb is not None:
            try:
                self._progress_cb(name=name, outcome=outcome, reason=reason)
            except Exception as exc:
                logger.debug("progress callback failed: %s", exc)

    # ----- run loop -----------------------------------------------------------

    def run(self, out_dir: Path) -> PipelineResult:
        out_dir.mkdir(parents=True, exist_ok=True)

        token = resolve_github_token(self.input.repo, self.input.auth)
        if self.input.repo.access == "private" and not token:
            raise RuntimeError(
                "private repo specified but no GitHub token resolved. "
                "Run `gh auth login` or set GITHUB_TOKEN."
            )

        owner, name = self.input.repo.owner_name
        logger.info("listing merged PRs for %s/%s (limit=%d)", owner, name, self.options.limit)
        try:
            prs = list_merged_prs(
                owner,
                name,
                limit=self.options.limit,
                since=self.options.since,
                until=self.options.until,
                skip_drafts=self.options.skip_drafts,
                token=token,
            )
        except GitHubError as exc:
            raise RuntimeError(f"failed to list PRs: {exc}") from exc

        skip_reasons: dict[str, int] = {}
        emitted = 0
        sandbox = None  # lazy-init for the validation loop

        try:
            for pr in prs:
                pr_label = f"{owner}/{name}#{pr.number}"

                # Pre-validation skip filters
                reason = self._pre_filter(pr)
                if reason:
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    self._emit_progress(pr_label, "skip", reason)
                    continue

                # Fetch diff
                try:
                    diff = fetch_pr_diff(owner, name, pr.number, token=token)
                except GitHubError as exc:
                    logger.warning("PR #%d: diff fetch failed: %s", pr.number, exc)
                    skip_reasons["diff_fetch_failed"] = skip_reasons.get("diff_fetch_failed", 0) + 1
                    self._emit_progress(pr_label, "error", "diff_fetch_failed")
                    continue

                patch, test_patch = split_patch_and_test_patch(diff)
                if not patch.strip():
                    skip_reasons["empty_source_patch"] = (
                        skip_reasons.get("empty_source_patch", 0) + 1
                    )
                    self._emit_progress(pr_label, "skip", "empty_source_patch")
                    continue
                if not test_patch.strip():
                    skip_reasons["no_test_patch"] = skip_reasons.get("no_test_patch", 0) + 1
                    self._emit_progress(pr_label, "skip", "no_test_patch")
                    continue

                # Structural quality filters (cheap, run before validation):
                # - CI-only PRs: source patch is 100% under .github/
                # - No new test functions: test_patch only edits comments/docstrings
                structural_reason = self._structural_quality_filter(patch, test_patch)
                if structural_reason:
                    skip_reasons[structural_reason] = skip_reasons.get(structural_reason, 0) + 1
                    self._emit_progress(pr_label, "skip", structural_reason)
                    continue

                # Lite-style structural filters
                lite_reason = self._lite_filter(pr, patch)
                if lite_reason:
                    skip_reasons[lite_reason] = skip_reasons.get(lite_reason, 0) + 1
                    self._emit_progress(pr_label, "skip", lite_reason)
                    continue

                # Validation (optional via skip_validation)
                fail_to_pass: list[str] = []
                pass_to_pass: list[str] = []
                validation_status = "skipped"
                if not self.options.skip_validation:
                    if sandbox is None:
                        sandbox = self._start_validation_sandbox()
                    from repo2rlenv.pipelines.pr_runtime_validate import validate_pr

                    targeted_cmds = targeted_test_cmds_for_pr(
                        normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
                        _files_in_patch(test_patch),
                    )
                    outcome = validate_pr(
                        sandbox=sandbox,
                        base_commit=pr.base_sha,
                        patch=patch,
                        test_patch=test_patch,
                        test_cmds=targeted_cmds,
                        language=self.bootstrap.language.value,
                        timeout=self.options.validation_timeout_sec,
                    )
                    fail_to_pass = outcome.fail_to_pass
                    pass_to_pass = outcome.pass_to_pass
                    validation_status = outcome.status
                    if (
                        self.options.require_fail_to_pass
                        and len(fail_to_pass) < self.options.min_fail_to_pass
                    ):
                        skip_reasons["no_fail_to_pass"] = skip_reasons.get("no_fail_to_pass", 0) + 1
                        self._emit_progress(pr_label, "skip", outcome.reason or "no_fail_to_pass")
                        continue

                # Emit the Harbor task
                task = self._build_task(
                    pr,
                    patch,
                    test_patch,
                    fail_to_pass=fail_to_pass,
                    pass_to_pass=pass_to_pass,
                    validation_status=validation_status,
                )
                write_harbor_task(task, out_dir)
                emitted += 1
                logger.info(
                    "emitted task %s (F2P=%d, P2P=%d)",
                    task.name,
                    len(fail_to_pass),
                    len(pass_to_pass),
                )
                self._emit_progress(task.name, "emit")
        finally:
            if sandbox is not None:
                sandbox.cleanup()

        return PipelineResult(
            candidates=len(prs),
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # ----- filters ------------------------------------------------------------

    def _pre_filter(self, pr: PullRequestSummary) -> str | None:
        """Cheap filters that don't need the diff."""
        if pr.is_draft and self.options.skip_drafts:
            return "draft"
        if not pr.merged_at:
            return "not_merged"
        if not pr.changed_files:
            return "no_files"
        if (
            self.options.min_problem_statement_words > 0
            and _word_count(pr.body or "") < self.options.min_problem_statement_words
        ):
            return "problem_statement_too_short"
        return None

    def _structural_quality_filter(self, source_patch: str, test_patch: str) -> str | None:
        """Cheap diff-level filters that catch over-emitted task types.

        Returns a skip reason string, or None to keep.

        Two filters here, both shipping a lot of false positives in v0.3:
          1. CI-only PRs: source patch is 100% under .github/. These are
             tooling changes (zizmor scan, publish workflows) — no real
             code-fix signal.
          2. No new test functions: test_patch only edits comments /
             docstrings (e.g. typo cleanup PRs). Without a new test that
             FAILS at base_commit, there can be no FAIL_TO_PASS oracle.
        """
        source_files = _files_in_patch(source_patch)
        # Filter 1: CI-only — all source files are workflow YAMLs
        if (
            self.options.skip_ci_only
            and source_files
            and all(p.startswith(".github/") for p in source_files)
        ):
            return "ci_only_patch"
        # Filter 2: test_patch must add ≥1 new test function
        if self.options.require_new_test_funcs:
            n_new = _count_new_test_funcs(test_patch)
            if n_new < 1:
                return "no_new_test_funcs"
        return None

    def _lite_filter(self, pr: PullRequestSummary, source_patch: str) -> str | None:
        """SWE-bench Lite-style structural filters."""
        source_files = _files_in_patch(source_patch)
        if len(source_files) > self.options.max_source_files_per_pr:
            return "too_many_source_files"
        if self.options.lite_filter:
            if len(source_files) != 1:
                return "lite_not_single_source_file"
            if _word_count(pr.body or "") < 40:
                return "lite_problem_too_short"
            # Reject if PR body contains images / external links / cross-PR/issue refs
            body = (pr.body or "").lower()
            if re.search(r"!\[[^\]]*\]\(|<img\s", body):
                return "lite_has_image"
            if re.search(r"\bhttps?://(?!github\.com/)", body):
                return "lite_has_external_link"
            if re.search(r"\b[a-f0-9]{7,40}\b", body):
                return "lite_has_commit_sha"
        return None

    # ----- sandbox -----------------------------------------------------------

    def _start_validation_sandbox(self):
        """Spin up a DockerSandbox from the bootstrap image (shared across PRs)."""
        # The bootstrap image already contains the repo at the bootstrap-time HEAD.
        # We pass repo_dir=None? No — DockerSandbox.start requires a repo_dir to copy in.
        # The repo is already in the image; we'll just `git checkout` to each PR's base_commit
        # from inside the container, so the path here is a no-op marker.
        import tempfile

        from repo2rlenv.bootstrap.docker import DockerSandbox

        marker = Path(tempfile.mkdtemp(prefix="r2e-pr-runtime-"))
        (marker / ".keep").write_text("")  # docker cp <src>/. <dst> works on any non-empty dir
        # Pull just the tag, don't re-copy the repo (image already has it)
        sandbox = DockerSandbox.start(
            base_image=self.bootstrap.image_tag,
            repo_dir=marker,
            platform=self.input.bootstrap.platform,
        )
        return sandbox

    # ----- task builder -------------------------------------------------------

    def _build_task(
        self,
        pr: PullRequestSummary,
        patch: str,
        test_patch: str,
        *,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
        validation_status: str,
    ) -> HarborTask:
        owner, name = self.input.repo.owner_name
        task_id = f"{owner}__{name}-{pr.number}"

        eval_script = build_eval_script(
            base_commit=pr.base_sha,
            test_patch=test_patch,
            test_cmds=targeted_test_cmds_for_pr(
                normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
                _files_in_patch(test_patch),
            ),
            language=self.bootstrap.language.value,
        )
        # Use image_tag for local-only bootstraps (Docker can resolve locally),
        # image_digest only when the image is in a registry that BuildKit can
        # actually pull from.
        image_ref = (
            self.bootstrap.image_digest
            if self.bootstrap.pushed_to_registry
            else self.bootstrap.image_tag
        )
        dockerfile = build_environment_dockerfile(
            bootstrap_image=image_ref,
            base_commit=pr.base_sha,
        )

        repo2env = {
            "pipeline": "pr_runtime",
            "pipeline_version": "0.3.0",
            "repo": f"{owner}/{name}",
            "ref": pr.base_sha,
            "reference": pr.url,
            "source_access": self.input.repo.access,
            "built_at": datetime.now(UTC).isoformat(),
            "synthesis_llm": self.input.llm.qualified_name,
            "reward_kinds": ["test_execution", "diff_similarity"],
            "pr_runtime": {
                "pr_url": pr.url,
                "pr_merged_at": pr.merged_at,
                "base_commit": pr.base_sha,
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "validation_status": validation_status,
                "bootstrap_image": self.bootstrap.image_digest,
            },
        }

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=pr.title or task_id,
            instruction=_build_instruction(pr),
            oracle_diff=patch,
            repo2env=repo2env,
            difficulty="medium",
            category="bugfix",
            keywords=[name, "pr_runtime"],
            environment_dockerfile=dockerfile,
            test_script=eval_script,
        )
