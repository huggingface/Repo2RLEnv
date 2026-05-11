"""Synthetic bug injection (SWE-smith-style).

Procedurally mutates Python source files with AST rewrites, keeps only the
mutations that break ≥1 existing test (and ≤ `max_tests_broken` to filter
out catastrophic mutations like flipping a flag in an import path), asks
the LLM for a problem statement that describes the symptom WITHOUT
revealing the fix, and emits a Harbor task whose:

  - environment/Dockerfile bakes the MUTATED state into the image (FROM
    bootstrap + heredoc-apply the mutation diff)
  - solution/patch.diff is the INVERSE mutation (restores original source)
  - tests/test.sh runs the targeted broken tests

This is the first **synthesized** pipeline. Unlike `pr_runtime` /
`commit_runtime`, the data does not come from upstream history; it's
manufactured on demand from any repo's existing test coverage. Yield is
high (~dozens of tasks per repo) but task quality varies — the LLM-judged
QA gate (planned in a follow-up) will filter out trivial or unrealistic
bugs.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
Inspired by:

  SWE-smith: Scaling Data for Software Engineering Agents
  (Yang et al., NeurIPS '25 Spotlight, arXiv:2504.21798)
  https://github.com/SWE-bench/SWE-smith        (MIT)

The mutation-then-filter recipe (procedural AST modifiers + "keep when
≥1 test fails" gate + LLM-authored issue text that hides the fix) is
adapted from SWE-smith's bug-gen pipeline. No code is copied; the operator
implementations and orchestration are original Python-stdlib code.

Released under Apache-2.0 along with the rest of Repo2RLEnv.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import base64
import difflib
import fnmatch
import hashlib
import logging
import random
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_github_token
from repo2rlenv.bootstrap.runner import _shallow_clone_at_ref
from repo2rlenv.bootstrap.spec import BootstrapResult
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.llm import complete
from repo2rlenv.log_parsers import parse_logs
from repo2rlenv.pipelines._mutation_operators import (
    Mutation,
    apply_to_source,
    find_all_mutations,
)
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.pr_runtime import (
    _path_prelude_for_language,
    normalize_test_cmds_for_runtime,
)
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import MutationBugsOptions

logger = logging.getLogger(__name__)


_ISSUE_SYSTEM_PROMPT = """You are a user filing a GitHub issue against a Python open-source project.

A bug has been introduced in the code. You are shown a list of failing tests and the truncated test output. Your job: write the issue text that a NORMAL USER would file, focused on the SYMPTOM.

STRICT REQUIREMENTS:
- DO NOT mention "tests" or "test failures" — a regular user wouldn't say that.
- DO NOT reveal the fix or suggest a code change.
- DO NOT mention specific function or class names from the failing test names verbatim.
- DO NOT include code snippets that contain the fix.
- DO NOT include a stack trace or pytest output.
- Describe what the user EXPECTED to happen vs what HAPPENED, in plain prose.
- Keep it under ~150 words. Two short paragraphs is typical.
- Output ONLY the issue body — no title, no markdown headers, no preamble.

Write the issue body now."""


@dataclass(slots=True)
class _MutationOutcome:
    """Per-candidate validation result."""

    broken_tests: list[str] = field(default_factory=list)
    failed_after_mutation: list[str] = field(default_factory=list)
    test_output: str = ""
    accepted: bool = False
    reason: str = ""


def _make_unified_diff(old: str, new: str, path: str) -> str:
    """Build a unified diff with a `diff --git` header so `git apply` accepts it.

    Normalizes trailing newlines BEFORE diffing — without this, when one side
    of the diff is missing a trailing `\\n` (common with `ast.unparse` output),
    Python's `difflib.unified_diff` yields adjacent `- foo` and `+ foo\\n`
    items WITHOUT emitting the `\\ No newline at end of file` marker, and the
    naive `"".join(...)` then glues them into a corrupt line like
    `- foo+ foo\\n`. Real-world `git apply` rejects such patches outright.
    """
    if not old.endswith("\n"):
        old = old + "\n"
    if not new.endswith("\n"):
        new = new + "\n"
    if old == new:
        return ""
    lines = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    if not lines:
        return ""
    body = "".join(lines)
    if not body.endswith("\n"):
        body += "\n"
    return f"diff --git a/{path} b/{path}\n{body}"


def _is_excluded(relative_path: str, exclude_globs: list[str]) -> bool:
    """True if `relative_path` matches any glob in `exclude_globs`."""
    return any(fnmatch.fnmatch(relative_path, pat) for pat in exclude_globs)


def _slice_test_output(output: str) -> str:
    """Trim to the section between START_TEST_OUTPUT / END_TEST_OUTPUT markers."""
    start = output.find("START_TEST_OUTPUT")
    end = output.find("END_TEST_OUTPUT")
    if start == -1:
        return output
    chunk = output[start:end] if end > start else output[start:]
    nl = chunk.find("\n")
    return chunk[nl + 1 :] if nl != -1 else chunk


def _target_pytest_for_tests(test_cmds: list[str], broken_tests: list[str]) -> list[str]:
    """For pytest commands, append the file paths derived from broken test names.

    A pytest test name looks like `tests/test_foo.py::test_bar` or
    `tests/test_foo.py::TestClass::test_method`. We extract the unique file
    paths (the part before `::`) and append them as positional args. Tests
    that fail without a file-path prefix (e.g. parametrize ids) are skipped;
    if no file paths survive, we leave the cmd alone (full-suite fallback).
    """
    import re

    file_paths: list[str] = []
    seen: set[str] = set()
    for name in broken_tests:
        # Names without :: are unparseable for targeting; skip
        if "::" not in name:
            continue
        path = name.split("::", 1)[0]
        if path and path not in seen and path.endswith(".py"):
            seen.add(path)
            file_paths.append(path)
    if not file_paths:
        return test_cmds
    out: list[str] = []
    for cmd in test_cmds:
        if re.search(r"\bpytest\b", cmd):
            # Skip if cmd already targets paths
            has_path_arg = any(
                (t.endswith(".py") or "/" in t) and not t.startswith("-") for t in cmd.split()[1:]
            )
            if not has_path_arg:
                cmd = cmd.rstrip() + " " + " ".join(file_paths)
        out.append(cmd)
    return out


def build_mutation_environment_dockerfile(bootstrap_image: str, mutation_diff: str) -> str:
    """Build a per-task Dockerfile that FROMs bootstrap + applies the mutation.

    Uses base64 to embed the mutation diff inline. Safer than heredocs:
    no escaping concerns for ANY diff content (no $$, no backslashes,
    no terminator collisions).
    """
    encoded = base64.b64encode(mutation_diff.encode("utf-8")).decode("ascii")
    return (
        f"# Auto-generated by Repo2RLEnv mutation_bugs\n"
        f"FROM {bootstrap_image}\n"
        f"WORKDIR /workspace\n"
        f"# Defensive git install (bootstrap base images vary)\n"
        f"RUN command -v git >/dev/null 2>&1 || \\\n"
        f"    (apt-get update && apt-get install -y --no-install-recommends git \\\n"
        f"     && rm -rf /var/lib/apt/lists/*) || \\\n"
        f"    apk add --no-cache git || true\n"
        f"RUN git config --global --add safe.directory /workspace\n"
        f"# Bake the mutation into the image so the agent sees broken code on start.\n"
        f"# Base64 keeps the diff content opaque to Docker's parser.\n"
        f"RUN echo {encoded} | base64 -d > /tmp/r2e_mutation.patch \\\n"
        f"    && cd /workspace && git apply --verbose --reject /tmp/r2e_mutation.patch \\\n"
        f"    && rm /tmp/r2e_mutation.patch\n"
    )


def build_mutation_eval_script(test_cmds: list[str], *, language: str | None = None) -> str:
    """Build `tests/test.sh` for a mutation_bugs task.

    Simpler than pr_runtime's: no test_patch to apply (we already targeted
    the failing tests via the pytest invocation). Just runs the commands
    wrapped in START/END markers and writes 1.0/0.0 to /logs/verifier/reward.txt.
    """
    test_block = " && ".join(test_cmds) if test_cmds else "echo 'no test_cmds configured'"
    path_prelude = _path_prelude_for_language(language)
    return (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        f"{path_prelude}"
        "cd /workspace\n"
        "git config --global --add safe.directory /workspace\n"
        "mkdir -p /logs/verifier\n"
        ": 'START_TEST_OUTPUT'\n"
        f"{test_block}\n"
        "TEST_EXIT_CODE=$?\n"
        ": 'END_TEST_OUTPUT'\n"
        '[ "$TEST_EXIT_CODE" -eq 0 ] && echo "1.0" > /logs/verifier/reward.txt '
        '|| echo "0.0" > /logs/verifier/reward.txt\n'
        "exit $TEST_EXIT_CODE\n"
    )


class MutationBugsPipeline:
    """Procedurally inject Python bugs + validate via test suite."""

    name: ClassVar[PipelineName] = PipelineName.MUTATION_BUGS
    requires_bootstrap: ClassVar[bool] = True

    def __init__(
        self,
        input: GenerationInput,
        options: MutationBugsOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "mutation_bugs requires a BootstrapResult (set requires_bootstrap=True "
                "and let cmd_generate trigger it, or pass one explicitly)"
            )
        self.input = input
        self.options = options
        self.bootstrap = bootstrap
        self._progress_cb = None
        self._llm_cost_usd = 0.0

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

        rng = random.Random(self.options.seed) if self.options.seed is not None else random.Random()
        skip_reasons: dict[str, int] = {}
        emitted = 0
        candidates_seen = 0
        sandbox = None

        with tempfile.TemporaryDirectory(prefix="r2e-mutation-bugs-") as tmp:
            clone_dir = Path(tmp) / "repo"
            logger.info("cloning %s for mutation discovery", self.input.repo.url)
            try:
                _shallow_clone_at_ref(
                    self.input.repo.url,
                    self.input.repo.ref,
                    token,
                    clone_dir,
                    depth=1,
                )
            except Exception as exc:
                raise RuntimeError(f"failed to clone {self.input.repo.url}: {exc}") from exc

            # Walk source files matching file_glob, excluding test / docs paths
            source_files = self._discover_source_files(clone_dir)
            logger.info("mutation_bugs: %d candidate source files", len(source_files))
            rng.shuffle(source_files)

            try:
                if not self.options.skip_validation:
                    sandbox = self._start_sandbox()
                    baseline_passed = self._compute_baseline(sandbox)
                    logger.info(
                        "mutation_bugs: baseline has %d passing tests", len(baseline_passed)
                    )
                else:
                    baseline_passed = set()

                for src_file in source_files:
                    if emitted >= self.options.limit:
                        break
                    relative = str(src_file.relative_to(clone_dir))
                    label = f"{owner_name}:{relative}"

                    try:
                        source = src_file.read_text(encoding="utf-8", errors="replace")
                    except OSError as exc:
                        logger.debug("read failed for %s: %s", relative, exc)
                        skip_reasons["read_error"] = skip_reasons.get("read_error", 0) + 1
                        self._emit_progress(label, "skip", "read_error")
                        continue

                    mutations = find_all_mutations(source, self.options.operators)
                    if not mutations:
                        skip_reasons["no_mutation_sites"] = (
                            skip_reasons.get("no_mutation_sites", 0) + 1
                        )
                        self._emit_progress(label, "skip", "no_mutation_sites")
                        continue
                    rng.shuffle(mutations)

                    attempted = 0
                    for mutation in mutations:
                        if attempted >= self.options.max_attempts_per_file:
                            break
                        if emitted >= self.options.limit:
                            break
                        attempted += 1
                        candidates_seen += 1

                        try:
                            mutated_source = apply_to_source(mutation, source)
                        except (SyntaxError, ValueError) as exc:
                            logger.debug("mutation apply failed: %s", exc)
                            continue
                        if mutated_source == source:
                            continue  # operator was a no-op for this site

                        mutation_diff = _make_unified_diff(source, mutated_source, relative)
                        if not mutation_diff:
                            continue
                        gold_diff = _make_unified_diff(mutated_source, source, relative)

                        outcome = self._validate_mutation(
                            sandbox=sandbox,
                            in_container_path=f"/workspace/{relative}",
                            mutated_source=mutated_source,
                            original_source=source,
                            baseline_passed=baseline_passed,
                        )
                        if not outcome.accepted:
                            skip_reasons[outcome.reason] = skip_reasons.get(outcome.reason, 0) + 1
                            self._emit_progress(label, "skip", outcome.reason)
                            continue

                        # LLM authors the issue text — unless skip_validation
                        # (in which case there are no real broken tests to describe;
                        # use a placeholder so emission still works for debug).
                        if self.options.skip_validation:
                            instruction = (
                                f"# Debug task (skip_validation=true)\n\n"
                                f"A {mutation.operator} mutation was applied at "
                                f"{relative}:{mutation.lineno} ({mutation.description}). "
                                f"This task was emitted without test validation; the "
                                f"oracle restores the original source. Re-run without "
                                f"`skip_validation` to get a proper LLM-authored "
                                f"problem statement."
                            )
                        else:
                            instruction = self._author_issue_text(
                                broken_tests=outcome.broken_tests,
                                test_output=outcome.test_output,
                            )
                            if not instruction:
                                skip_reasons["llm_failed"] = skip_reasons.get("llm_failed", 0) + 1
                                self._emit_progress(label, "skip", "llm_failed")
                                continue

                        task = self._build_task(
                            file_path=relative,
                            mutation=mutation,
                            mutation_diff=mutation_diff,
                            gold_diff=gold_diff,
                            instruction=instruction,
                            broken_tests=outcome.broken_tests,
                        )
                        write_harbor_task(task, out_dir)
                        emitted += 1
                        logger.info(
                            "emitted task %s (operator=%s, broken=%d)",
                            task.name,
                            mutation.operator,
                            len(outcome.broken_tests),
                        )
                        self._emit_progress(task.name, "emit")
                        break  # one mutation per file is enough
            finally:
                if sandbox is not None:
                    sandbox.cleanup()
                shutil.rmtree(clone_dir, ignore_errors=True)

        return PipelineResult(
            candidates=candidates_seen,
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # ----- discovery ----------------------------------------------------------

    def _discover_source_files(self, clone_dir: Path) -> list[Path]:
        """Walk the local clone for files matching file_glob, minus excludes."""
        all_matches = list(clone_dir.glob(self.options.file_glob))
        out: list[Path] = []
        for p in all_matches:
            if not p.is_file():
                continue
            rel = str(p.relative_to(clone_dir))
            if _is_excluded(rel, self.options.exclude_glob):
                continue
            out.append(p)
        return out

    # ----- sandbox -----------------------------------------------------------

    def _start_sandbox(self):
        """Open a long-lived sandbox from the bootstrap image."""
        from repo2rlenv.bootstrap.docker import DockerSandbox

        marker = Path(tempfile.mkdtemp(prefix="r2e-mut-bugs-"))
        (marker / ".keep").write_text("")
        return DockerSandbox.start(
            base_image=self.bootstrap.image_tag,
            repo_dir=marker,
            platform=self.input.bootstrap.platform,
        )

    def _scoped_test_cmds(self) -> list[str]:
        """Bootstrap test_cmds + optional `test_target` positional arg.

        When the user sets `test_target` we append it to every pytest
        invocation so generation-time runs stay fast (one file instead of
        the full suite). The targeted-test logic in `_target_pytest_for_tests`
        (used for the emitted verifier) still narrows to the actually-broken
        test files; this option only affects discovery/validation speed.
        """
        normalized = normalize_test_cmds_for_runtime(self.bootstrap.test_cmds)
        target = (self.options.test_target or "").strip()
        if not target:
            return normalized
        import re

        out: list[str] = []
        for cmd in normalized:
            if re.search(r"\bpytest\b", cmd) and not re.search(r"\s\S*\.py(::|\b)", cmd):
                cmd = cmd.rstrip() + " " + target
            out.append(cmd)
        return out

    def _compute_baseline(self, sandbox) -> set[str]:
        """Run the test suite once and return the set of PASSED test names."""
        normalized = self._scoped_test_cmds()
        if not normalized:
            return set()
        cmd = " && ".join(normalized)
        script = (
            "set -uxo pipefail\n"
            "cd /workspace\n"
            "git config --global --add safe.directory /workspace\n"
            "git reset --hard HEAD\n"
            "git clean -fdx -e .venv -e venv -e __pycache__ || true\n"
            ": 'START_TEST_OUTPUT'\n"
            f"{cmd} || true\n"
            ": 'END_TEST_OUTPUT'\n"
        )
        r = sandbox.exec(script, timeout=self.options.validation_timeout_sec)
        # Parse stdout directly. Test output is on stdout; combining stderr
        # would interleave `set -x` trace lines (which contain our START/END
        # markers) AFTER the real test output, and _slice_test_output would
        # then trim to the stderr trace region and lose every PASSED line.
        status = parse_logs(normalized, r.stdout, language=self.bootstrap.language.value)
        logger.info(
            "mutation_bugs baseline: parsed %d test entries (%d passing) from %d-byte stdout",
            len(status),
            sum(1 for s in status.values() if s == "PASSED"),
            len(r.stdout),
        )
        return {name for name, s in status.items() if s == "PASSED"}

    def _validate_mutation(
        self,
        *,
        sandbox,
        in_container_path: str,
        mutated_source: str,
        original_source: str,
        baseline_passed: set[str],
    ) -> _MutationOutcome:
        """Bake the mutated source into the running container, run tests, classify.

        Returns an outcome describing which tests broke. The original source
        is restored on the way out (success OR failure) so the next candidate
        starts from a clean state.
        """
        if self.options.skip_validation or sandbox is None:
            # Caller wants emission without execution — treat as accepted with
            # zero broken tests. The emitted task will still apply the diff,
            # but no F2P oracle. Useful for debug.
            return _MutationOutcome(accepted=True, reason="skipped")

        encoded_mut = base64.b64encode(mutated_source.encode("utf-8")).decode("ascii")
        encoded_orig = base64.b64encode(original_source.encode("utf-8")).decode("ascii")

        normalized = self._scoped_test_cmds()
        if not normalized:
            return _MutationOutcome(accepted=False, reason="no_test_cmds")
        cmd = " && ".join(normalized)
        script = (
            "set -uxo pipefail\n"
            "cd /workspace\n"
            "git config --global --add safe.directory /workspace\n"
            # Apply mutation
            f"echo {encoded_mut} | base64 -d > {in_container_path}\n"
            ": 'START_TEST_OUTPUT'\n"
            f"{cmd} || true\n"
            ": 'END_TEST_OUTPUT'\n"
            # Restore original
            f"echo {encoded_orig} | base64 -d > {in_container_path}\n"
        )
        r = sandbox.exec(script, timeout=self.options.validation_timeout_sec)
        if not r.ok and r.exit_code == 124:
            return _MutationOutcome(accepted=False, reason="validation_timeout")
        status = parse_logs(normalized, r.stdout, language=self.bootstrap.language.value)

        failed_after = [name for name, s in status.items() if s == "FAILED"]
        broken = [name for name in failed_after if name in baseline_passed]
        # 4 KB tail of stdout is what we'll show the LLM — pytest's summary
        # block lives there.
        log_tail = r.stdout[-4000:] if r.stdout else ""

        if len(broken) < self.options.min_tests_broken:
            return _MutationOutcome(
                accepted=False,
                reason="too_few_broken",
                failed_after_mutation=failed_after,
                test_output=log_tail,
            )
        if len(broken) > self.options.max_tests_broken:
            return _MutationOutcome(
                accepted=False,
                reason="too_many_broken",
                failed_after_mutation=failed_after,
                test_output=log_tail,
            )
        return _MutationOutcome(
            accepted=True,
            broken_tests=sorted(broken),
            failed_after_mutation=failed_after,
            test_output=log_tail,
        )

    # ----- LLM ---------------------------------------------------------------

    def _author_issue_text(self, *, broken_tests: list[str], test_output: str) -> str:
        """Ask the LLM for a user-shaped issue body describing the symptom."""
        # Truncate inputs to fit within the LLM context budget
        tests_block = "\n".join(f"  - {t}" for t in broken_tests[:5])
        output_block = test_output[-2000:] if test_output else ""
        user_prompt = (
            "Failing tests:\n"
            f"{tests_block}\n\n"
            "Truncated test output (for your reference; do not quote it):\n"
            "```\n"
            f"{output_block}\n"
            "```\n\n"
            "Write the issue body now."
        )
        try:
            resp = complete(
                self.input.llm,
                system=_ISSUE_SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=self.options.max_llm_tokens,
                temperature=self.options.llm_temperature,
            )
        except Exception as exc:
            logger.warning("LLM call failed: %s", exc)
            return ""
        self._llm_cost_usd += resp.cost_usd
        return resp.content.strip()

    # ----- task builder -------------------------------------------------------

    def _build_task(
        self,
        *,
        file_path: str,
        mutation: Mutation,
        mutation_diff: str,
        gold_diff: str,
        instruction: str,
        broken_tests: list[str],
    ) -> HarborTask:
        owner, name = self.input.repo.owner_name
        # Stable, content-derived task ID
        h = hashlib.sha256()
        h.update(file_path.encode())
        h.update(b"\0")
        h.update(mutation.operator.encode())
        h.update(b"\0")
        h.update(mutation.description.encode())
        h.update(b"\0")
        h.update(str(mutation.lineno).encode())
        task_id = f"{owner}__{name}-mut-{h.hexdigest()[:8]}"

        # Target only the broken tests so other unrelated suite failures don't
        # contaminate the verifier signal
        targeted_cmds = _target_pytest_for_tests(
            normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
            broken_tests,
        )
        eval_script = build_mutation_eval_script(
            targeted_cmds, language=self.bootstrap.language.value
        )
        image_ref = (
            self.bootstrap.image_digest
            if self.bootstrap.pushed_to_registry
            else self.bootstrap.image_tag
        )
        dockerfile = build_mutation_environment_dockerfile(
            bootstrap_image=image_ref,
            mutation_diff=mutation_diff,
        )

        repo2env = {
            "pipeline": "mutation_bugs",
            "pipeline_version": "0.6.0",
            "repo": f"{owner}/{name}",
            "ref": self.input.repo.ref,
            "reference": f"https://github.com/{owner}/{name}/blob/{self.input.repo.ref}/{file_path}",
            "source_access": self.input.repo.access,
            "built_at": datetime.now(UTC).isoformat(),
            "synthesis_llm": self.input.llm.qualified_name,
            "reward_kinds": ["test_execution", "diff_similarity"],
            "mutation_bugs": {
                "file_path": file_path,
                "operator": mutation.operator,
                "operator_description": mutation.description,
                "lineno": mutation.lineno,
                "broken_tests": broken_tests,
                "bootstrap_image": self.bootstrap.image_digest,
                "seed": self.options.seed,
                "llm_cost_usd": round(self._llm_cost_usd, 6),
            },
        }

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=f"Fix the bug in {file_path}",
            instruction=instruction,
            oracle_diff=gold_diff,
            repo2env=repo2env,
            difficulty="medium",
            category="bugfix",
            keywords=[name, "mutation_bugs", mutation.operator],
            environment_dockerfile=dockerfile,
            test_script=eval_script,
        )
