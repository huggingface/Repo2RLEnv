"""Rename-refactor mining (Python-native, no JVM).

Walks the target repo's commit history; for each commit whose message
matches a "rename X to Y" pattern, fetches the diff via `git show` and
verifies the diff actually performs the rename. Emits a Harbor task
with:

  - environment/Dockerfile: FROM bootstrap, reset to the parent commit
    (pre-rename state)
  - solution/patch.diff: the historical rename commit's source diff
  - tests/test.sh: multi-criteria verifier
      (1) structural: old name absent in `src/`, new name present
      (2) behavioral: existing test suite still passes

Unlike `commit_runtime`, the verifier here doesn't depend on a
test_patch — the rename touches source code, and the existing suite is
the behavioral oracle. The structural grep guards against the trivial
"agent changed nothing" cheat.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The v1.0 spec for this pipeline (docs/pipelines/refactor_synthesis.md
pre-v0.8) called for [RefactoringMiner](https://github.com/tsantalis/RefactoringMiner),
a JVM-based detector. v0.8 drops the JVM dependency and uses a
commit-message + diff-verification recipe. We miss unannounced renames
(higher false-negative rate) but eliminate the cross-runtime cost.

The recipe is original Python stdlib; no code is copied. Released under
Apache-2.0 along with the rest of Repo2RLEnv.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import hashlib
import logging
import re
import shlex
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_github_token
from repo2rlenv.bootstrap.runner import _shallow_clone_at_ref
from repo2rlenv.bootstrap.spec import BootstrapResult
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.git_local import CommitInfo, GitError, list_commits, show_diff
from repo2rlenv.pipelines._rename_detector import (
    find_rename_in_message,
    verify_rename_in_diff,
)
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.pr_runtime import (
    _files_in_patch,
    _path_prelude_for_language,
    build_environment_dockerfile,
    normalize_test_cmds_for_runtime,
    split_patch_and_test_patch,
    targeted_test_cmds_for_pr,
)
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import RefactorSynthesisOptions

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Multi-criteria verifier script
# ---------------------------------------------------------------------------


# POSIX ERE — `\s` isn't portable in `grep -E`; use `[[:space:]]` instead.
# Matches `def NAME(...)`, `async def NAME(...)`, `class NAME(...)`, `class NAME:`.
# NOTE: the trailing char class is `[(:]` not `[:(]` — putting `(` first avoids
# `[:` getting parsed as the start of a POSIX class like `[:space:]` in ERE.
_DEFINITION_PATTERN_TEMPLATE = (
    r"^[[:space:]]*"
    r"(async[[:space:]]+def|def|class)"
    r"[[:space:]]+{name}"
    r"[[:space:]]*[(:]"
)


def build_rename_eval_script(
    *,
    test_cmds: list[str],
    old_name: str,
    new_name: str,
    require_old_gone: bool,
    require_new_present: bool,
    language: str | None = None,
) -> str:
    """Build `tests/test.sh` with structural + behavioral checks.

    Structural checks use `grep -RnE` over the entire working tree, scoped
    to common source directories. They run BEFORE the test suite so a
    trivial "do nothing" patch fails fast.

    The patterns match Python definitions only — assignments and calls
    are intentionally ignored (the rename may keep old call-site spellings
    intact in tests/docs without breaking the refactor's intent).
    """
    old_def = _DEFINITION_PATTERN_TEMPLATE.format(name=re.escape(old_name))
    new_def = _DEFINITION_PATTERN_TEMPLATE.format(name=re.escape(new_name))
    # Quote the patterns for the shell. We pass them as literals to grep -E.
    old_q = shlex.quote(old_def)
    new_q = shlex.quote(new_def)
    path_prelude = _path_prelude_for_language(language)

    # Search the whole working tree from `.` and let --include + --exclude-dir
    # do the narrowing. Passing a non-existent directory (e.g. `lib`, `pkg`)
    # as an explicit root makes grep exit 2 even when matches exist — `if !
    # grep` then treats that as "no match" and the structural check
    # spuriously fails. Searching `.` only avoids that.
    search_roots = "."
    grep_flags = (
        "--include='*.py' "
        "--exclude-dir=.git --exclude-dir=.venv --exclude-dir=venv "
        "--exclude-dir=__pycache__ --exclude-dir=build --exclude-dir=dist "
        "--exclude-dir=node_modules --exclude-dir=tests --exclude-dir=test "
        "--exclude-dir=docs --exclude-dir=examples"
    )

    # NOTE: error-message strings use SINGLE quotes around the name so bash
    # doesn't try to command-substitute a backtick'd identifier.
    structural_lines = []
    if require_old_gone:
        structural_lines.append(
            f"if grep -RnE {grep_flags} {old_q} {search_roots} 2>/dev/null; then\n"
            f"  echo \"STRUCTURAL FAIL: old name '{old_name}' still has a def/class in source\"\n"
            "  STRUCT_FAIL=1\n"
            "fi"
        )
    if require_new_present:
        structural_lines.append(
            f"if ! grep -RnE {grep_flags} {new_q} {search_roots} 2>/dev/null > /dev/null; then\n"
            f"  echo \"STRUCTURAL FAIL: new name '{new_name}' not defined anywhere in source\"\n"
            "  STRUCT_FAIL=1\n"
            "fi"
        )
    structural_block = "\n".join(structural_lines)

    # Rewrite a leading `pytest ` to `python -m pytest ` so the verifier
    # works even when the pytest entrypoint isn't on PATH in the
    # non-interactive shell. `python` is always available; `-m pytest`
    # loads the installed module either way.
    rewritten_cmds = [re.sub(r"\bpytest\b", "python -m pytest", c, count=1) for c in test_cmds]
    test_block = " && ".join(rewritten_cmds) if rewritten_cmds else "echo 'no test_cmds configured'"

    return (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        f"{path_prelude}"
        "cd /workspace\n"
        "git config --global --add safe.directory /workspace\n"
        "mkdir -p /logs/verifier\n"
        "STRUCT_FAIL=0\n"
        f"{structural_block}\n"
        'if [ "$STRUCT_FAIL" -ne 0 ]; then\n'
        '  echo "0.0" > /logs/verifier/reward.txt\n'
        "  exit 1\n"
        "fi\n"
        ": 'START_TEST_OUTPUT'\n"
        f"{test_block}\n"
        "TEST_EXIT_CODE=$?\n"
        ": 'END_TEST_OUTPUT'\n"
        '[ "$TEST_EXIT_CODE" -eq 0 ] && echo "1.0" > /logs/verifier/reward.txt '
        '|| echo "0.0" > /logs/verifier/reward.txt\n'
        "exit $TEST_EXIT_CODE\n"
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _build_instruction(
    *,
    old_name: str,
    new_name: str,
    kind: str,
    commit: CommitInfo,
    require_old_gone: bool,
) -> str:
    kind_label = kind or "symbol"
    old_gone_line = (
        f"- No `def {old_name}(...)` or `class {old_name}` should remain in the source tree.\n"
        if require_old_gone
        else (
            f"- The original `def {old_name}` / `class {old_name}` should be moved or replaced "
            f"by `{new_name}` (a backward-compat shim is acceptable).\n"
        )
    )
    return (
        f"# Rename `{old_name}` to `{new_name}`\n\n"
        f"Refactor the codebase to rename the {kind_label} `{old_name}` to `{new_name}`.\n"
        f"Update every definition and call-site so the existing test suite still passes "
        f"with the new name in place.\n\n"
        f"## Task\n\n"
        f"After your change:\n\n"
        f"{old_gone_line}"
        f"- A `def {new_name}(...)` or `class {new_name}` must exist somewhere in the source tree.\n"
        f"- The repository's existing test suite must pass.\n\n"
        f"For reference: this refactor was originally performed in commit "
        f"`{commit.sha[:12]}` ({commit.subject.strip()})."
    )


class RefactorSynthesisPipeline:
    """Rename-refactor mining + structural+behavioral verifier."""

    name: ClassVar[PipelineName] = PipelineName.REFACTOR_SYNTHESIS
    requires_bootstrap: ClassVar[bool] = True

    def __init__(
        self,
        input: GenerationInput,
        options: RefactorSynthesisOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "refactor_synthesis requires a BootstrapResult (set requires_bootstrap=True "
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
        owner_name = f"{owner}/{name}"
        skip_reasons: dict[str, int] = {}
        emitted = 0
        candidates: list[CommitInfo] = []

        with tempfile.TemporaryDirectory(prefix="r2e-refactor-synth-") as tmp:
            clone_dir = Path(tmp) / "repo"
            logger.info(
                "cloning %s @ %s (depth=%d) for rename mining",
                self.input.repo.url,
                self.input.repo.ref,
                self.options.clone_depth,
            )
            try:
                _shallow_clone_at_ref(
                    self.input.repo.url,
                    self.input.repo.ref,
                    token,
                    clone_dir,
                    depth=self.options.clone_depth,
                )
            except Exception as exc:
                raise RuntimeError(f"failed to clone {self.input.repo.url}: {exc}") from exc

            try:
                # Walk up to the clone depth — most commits won't match the
                # rename pattern, so we mine deep and filter heavily.
                candidates = list_commits(
                    clone_dir,
                    since=self.options.since,
                    until=self.options.until,
                    limit=self.options.clone_depth,
                    branch=self.options.branch,
                )
            except GitError as exc:
                raise RuntimeError(f"git log failed: {exc}") from exc
            logger.info(
                "refactor_synthesis: %d candidate commits in [%s, %s]",
                len(candidates),
                self.options.since,
                self.options.until,
            )

            try:
                for commit in candidates:
                    if emitted >= self.options.limit:
                        break
                    label = f"{owner_name}@{commit.sha[:12]}"

                    # Metadata-level filters (cheap)
                    if self.options.skip_merge_commits and commit.is_merge:
                        skip_reasons["merge_commit"] = skip_reasons.get("merge_commit", 0) + 1
                        continue
                    if commit.author_email in self.options.exclude_authors:
                        skip_reasons["excluded_author"] = skip_reasons.get("excluded_author", 0) + 1
                        continue
                    if not commit.parent_sha:
                        continue  # root commit; nothing to rename FROM

                    # Stage 1: commit message regex
                    matched = find_rename_in_message(commit.message)
                    if matched is None:
                        # Most commits don't match — silently skip; don't pollute
                        # skip_reasons with the bulk of the history.
                        continue
                    old_name, new_name, kind = matched

                    # Stage 2: fetch + verify the diff
                    try:
                        diff = show_diff(clone_dir, commit.sha)
                    except GitError as exc:
                        logger.warning("commit %s: git show failed: %s", commit.sha[:12], exc)
                        skip_reasons["diff_fetch_failed"] = (
                            skip_reasons.get("diff_fetch_failed", 0) + 1
                        )
                        self._emit_progress(label, "error", "diff_fetch_failed")
                        continue
                    outcome = verify_rename_in_diff(diff, old_name=old_name, new_name=new_name)
                    if not outcome.ok:
                        skip_reasons[f"diff_verify:{outcome.reason}"] = (
                            skip_reasons.get(f"diff_verify:{outcome.reason}", 0) + 1
                        )
                        self._emit_progress(label, "skip", outcome.reason)
                        continue

                    # Emit
                    task = self._build_task(
                        commit=commit,
                        diff=diff,
                        old_name=old_name,
                        new_name=new_name,
                        kind=kind,
                    )
                    write_harbor_task(task, out_dir)
                    emitted += 1
                    logger.info(
                        "emitted task %s (rename %s -> %s)",
                        task.name,
                        old_name,
                        new_name,
                    )
                    self._emit_progress(task.name, "emit")
            finally:
                shutil.rmtree(clone_dir, ignore_errors=True)

        return PipelineResult(
            candidates=len(candidates),
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # ----- task builder -------------------------------------------------------

    def _build_task(
        self,
        *,
        commit: CommitInfo,
        diff: str,
        old_name: str,
        new_name: str,
        kind: str,
    ) -> HarborTask:
        owner, name = self.input.repo.owner_name
        # Content-derived task id: stable hash over (old, new, commit sha)
        h = hashlib.sha256()
        h.update(old_name.encode())
        h.update(b"\0")
        h.update(new_name.encode())
        h.update(b"\0")
        h.update(commit.sha.encode())
        task_id = f"{owner}__{name}-rfn-{h.hexdigest()[:8]}"

        normalized_cmds = normalize_test_cmds_for_runtime(self.bootstrap.test_cmds)
        # If the rename commit touched test files, target only those — the
        # whole repo's test suite may have pre-existing failures unrelated to
        # the rename (e.g. test_arguments.py flaking on newer pytest). The
        # rename-affected tests are the meaningful behavioral signal.
        _, test_patch = split_patch_and_test_patch(diff)
        test_files = _files_in_patch(test_patch)
        if test_files:
            normalized_cmds = targeted_test_cmds_for_pr(normalized_cmds, test_files)
        eval_script = build_rename_eval_script(
            test_cmds=normalized_cmds,
            old_name=old_name,
            new_name=new_name,
            require_old_gone=self.options.require_old_name_gone,
            require_new_present=self.options.require_new_name_present,
            language=self.bootstrap.language.value,
        )
        image_ref = (
            self.bootstrap.image_digest
            if self.bootstrap.pushed_to_registry
            else self.bootstrap.image_tag
        )
        dockerfile = build_environment_dockerfile(
            bootstrap_image=image_ref,
            base_commit=commit.parent_sha,
        )

        repo2env = {
            "pipeline": "refactor_synthesis",
            "pipeline_version": "0.8.0",
            "repo": f"{owner}/{name}",
            "ref": commit.parent_sha,
            "reference": (f"https://github.com/{owner}/{name}/commit/{commit.sha}"),
            "source_access": self.input.repo.access,
            "built_at": datetime.now(UTC).isoformat(),
            "synthesis_llm": self.input.llm.qualified_name,
            "reward_kinds": ["test_execution", "diff_similarity"],
            "refactor_synthesis": {
                "refactor_kind": "rename",
                "rename_kind": kind,
                "old_name": old_name,
                "new_name": new_name,
                "commit_sha": commit.sha,
                "parent_sha": commit.parent_sha,
                "authored_at": commit.authored_at,
                "author_email": commit.author_email,
                "subject": commit.subject,
                "bootstrap_image": self.bootstrap.image_digest,
            },
        }

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=f"Rename {old_name} to {new_name}",
            instruction=_build_instruction(
                old_name=old_name,
                new_name=new_name,
                kind=kind,
                commit=commit,
                require_old_gone=self.options.require_old_name_gone,
            ),
            oracle_diff=diff,
            repo2env=repo2env,
            difficulty="easy",  # renames are mechanical
            category="refactor",
            keywords=[name, "refactor_synthesis", "rename"],
            environment_dockerfile=dockerfile,
            test_script=eval_script,
        )
