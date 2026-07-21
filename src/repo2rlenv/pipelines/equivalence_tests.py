"""R2E-style function-level equivalence-test synthesis.

For each module-level function in the target repo:

  1. Filter via AST (LOC range, has args + return, no obvious side effects)
  2. One LLM call asks for a pytest test that imports both `name` and
     `reference_name` from `task_module` and asserts equality across
     ≥5 inputs
  3. Verify two-stage in the bootstrap container:
       - Stage A: `name` stubbed (raise NotImplementedError), `reference_name`
                  is the original → test must FAIL
       - Stage B: `name = reference_name = original` → test must PASS
  4. Emit Harbor task whose gold patch adds `task_module.py` (with both
     `name` and `reference_name` set to the original implementation) plus
     a `test_r2e_<hash>.py` test file at the repo root.

Different from `code_instruct`:
  - The "problem" is grounded in a real function we extracted, not invented
  - The LLM only writes the test; we already have the ground truth
  - Yield scales with the number of qualifying functions in the repo

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
Inspired by:

  R2E: Turning any Github Repository into a Programming Agent Environment
  (Jain et al., ICML '24)
  https://github.com/r2e-project/r2e        (MIT)

The reference-oracle test pattern (`reference_<name>` as frozen oracle,
test compares both implementations) and the function-extractor filter
set are adapted from R2E's `repo_builder/fut_extractor/` + `generators/
testgen/prompt.py`. No code is copied; the implementation is original.

Released under Apache-2.0 along with the rest of Repo2RLEnv.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import base64
import hashlib
import logging
import random
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_repo_token
from repo2rlenv.bootstrap.runner import _shallow_clone_at_ref
from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.llm import complete
from repo2rlenv.pipelines._eval_script import (
    all_tests_passed,
    build_binary_eval_script,
    is_module_importable,
    make_unified_diff,
    rename_function_ast,
    signature_only_source,
    strip_annotations,
)
from repo2rlenv.pipelines._function_extractor import FunctionCandidate, walk_repo
from repo2rlenv.pipelines._oss_instruct import check_equivalence_test_strength
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import EquivalenceTestsOptions

logger = logging.getLogger(__name__)


PROMPT_SYSTEM = """You are a senior Python engineer writing differential equivalence tests.

You will be shown one function from an open-source library. Your job: write a single pytest test file that imports BOTH `<name>` and `reference_<name>` from the module `task_module`, and asserts they produce identical outputs across a set of inputs.

The reference implementation IS the ground truth. Do not try to write inputs that "trip up" the reference — pick inputs the reference clearly handles cleanly. The purpose of the test is to detect when a candidate diverges from the reference, not to exercise edge cases the reference itself doesn't handle.

STRICT REQUIREMENTS:
- Output ONE section labelled `[Test]` and nothing else (no preamble, no closing notes).
- Use plain `def test_*(): assert ...` style — NO unittest.TestCase, NO fixtures, NO mocks.
- The test MUST import both names from `task_module`: `from task_module import {name}, reference_{name}`.
- Write between 5 and 10 distinct `def test_*` functions, each with ONE assertion of the form:

      def test_name_case_N():
          expected = reference_{name}(<input>)
          actual = {name}(<input>)
          assert actual == expected

  Using this exact pattern is important — it means Stage-B (reference vs reference) is guaranteed to pass, and Stage-A (candidate stub raising vs reference) fails cleanly.
- Prefer inputs that produce clear, definite outputs. AVOID inputs where the reference would raise an exception; avoid inputs that require an ambient context (a Click context, a Flask request, filesystem state).
- Use literal Python values that can be constructed without third-party libraries. If the function takes complex types, build them in the test using only stdlib.
- DO NOT call any function other than `{name}` and `reference_{name}` (and Python builtins / stdlib).
- DO NOT redefine `{name}` or `reference_{name}` in the test.
- If the function's behavior depends on randomness, use `random.seed(0)` before each call.
"""


PROMPT_USER_TEMPLATE = """Reference function (from `{path}`, lines {start}-{end}):

```python
{source}
```

Signature args: {arg_names}.

Write the equivalence test now."""


@dataclass(slots=True)
class _VerifyOutcome:
    accepted: bool = False
    reason: str = ""
    stub_log: str = ""
    oracle_log: str = ""


@dataclass(slots=True)
class _ParsedTest:
    code: str  # the test file body


def _extract_test_section(text: str) -> _ParsedTest | None:
    """Extract `[Test]` section content from an LLM response. Returns None if missing."""
    m = re.search(r"(?im)^\s*\[\s*test\s*\]\s*$", text)
    if not m:
        return None
    body = text[m.end() :]
    # Stop at next `[Something Else]` section if present
    nxt = re.search(r"(?im)^\s*\[\s*[a-zA-Z][a-zA-Z ]+\s*\]\s*$", body)
    if nxt:
        body = body[: nxt.start()]
    # Strip a surrounding code fence
    fence = re.match(r"^\s*```(?:python|py)?\s*\n(.*?)\n```\s*$", body.strip(), re.DOTALL)
    if fence:
        body = fence.group(1)
    body = body.strip()
    if not body:
        return None
    return _ParsedTest(code=body)


_IMPORT_PATTERN = re.compile(
    r"^\s*(?:from\s+task_module\s+import|import\s+task_module)\b",
    re.MULTILINE,
)


def uses_both_names(test_code: str, name: str) -> bool:
    """True iff the test imports from task_module AND references both names.

    Defense against the LLM writing a trivial test (e.g., only uses
    `reference_<name>` and never calls `<name>` — which would pass even
    when `<name>` is stubbed).
    """
    if not _IMPORT_PATTERN.search(test_code):
        return False
    # Both names must appear as bare identifiers somewhere in the test
    name_used = re.search(rf"\b{re.escape(name)}\b", test_code) is not None
    ref_used = re.search(rf"\breference_{re.escape(name)}\b", test_code) is not None
    return name_used and ref_used


def _stub_module(candidate: FunctionCandidate) -> str:
    """Build `task_module.py` with `<name>` STUBBED + `reference_<name>` original.

    This is the STARTING state every agent sees: it's baked into the image
    (see `build_equivalence_dockerfile`) so `reference_<name>` is present for
    the agent to be equivalent to, and the agent's job is to fill in `<name>`.
    Also used in Stage A of verification (confirms the test actually exercises
    `<name>` — else it would pass while `<name>` raises NotImplementedError).

    Annotations on the extracted function are stripped so the module imports
    cleanly even when the original signature referenced repo-internal types
    (e.g. `def foo(x: Argument) -> FC` → `def foo(x)`). See
    `strip_annotations` in `_eval_script.py`.
    """
    ref_source = _rename_function_source(
        candidate.source, candidate.name, f"reference_{candidate.name}"
    )
    ref_source = strip_annotations(ref_source)
    args = ", ".join(candidate.arg_names) if candidate.arg_names else "*args, **kwargs"
    stub = (
        f"def {candidate.name}({args}):\n"
        f'    raise NotImplementedError("implement {candidate.name}")\n'
    )
    return ref_source + "\n\n" + stub


def build_equivalence_dockerfile(bootstrap_image: str, stub_module: str) -> str:
    """Per-task Dockerfile: FROM bootstrap + bake the stub `task_module.py`.

    Every agent starts from the stub (`reference_<name>` + a `<name>` that
    raises NotImplementedError), so the reference oracle the test compares
    against is present for non-oracle agents too — see issue #54. The gold
    patch is a stub→oracle modify-diff that fills in `<name>`.
    """
    encoded = base64.b64encode(stub_module.encode("utf-8")).decode("ascii")
    return (
        f"# Auto-generated by Repo2RLEnv equivalence_tests\n"
        f"FROM {bootstrap_image}\n"
        f"WORKDIR /workspace\n"
        f"RUN command -v git >/dev/null 2>&1 || \\\n"
        f"    (apt-get update && apt-get install -y --no-install-recommends git \\\n"
        f"     && rm -rf /var/lib/apt/lists/*) || \\\n"
        f"    apk add --no-cache git || true\n"
        f"RUN git config --global --add safe.directory /workspace\n"
        f"RUN echo {encoded} | base64 -d > /workspace/task_module.py\n"
    )


def _oracle_module(candidate: FunctionCandidate) -> str:
    """Build `task_module.py` with both `<name>` AND `reference_<name>` set to the original.

    Used in Stage B of verification AND as the gold-patch payload: the
    agent's job is to make `<name>` equivalent to `reference_<name>`;
    the gold patch achieves that by giving them the same implementation.

    Annotations are stripped for the same reason as `_stub_module`.
    """
    ref_source = _rename_function_source(
        candidate.source, candidate.name, f"reference_{candidate.name}"
    )
    ref_source = strip_annotations(ref_source)
    own_source = strip_annotations(candidate.source)
    return ref_source + "\n\n" + own_source + "\n"


def _rename_function_source(source: str, old_name: str, new_name: str) -> str:
    """Rename `def OLD(...)` → `def NEW(...)` in the source text — recursion-safe.

    v0.7 used a regex on the `def` line only, so recursive functions still
    called themselves by the old name inside the body → `reference_<name>`
    and `<name>` executed the same code and Stage B silently passed on
    bogus tasks. v0.8.7 uses `rename_function_ast` from `_eval_script.py`,
    which walks the AST and rewrites `Call(func=Name(id=old))` nodes and
    bare `Name` loads too. Falls back to the regex behaviour if AST
    parsing fails (source with tabs/mixed indent that ast rejects).
    """
    renamed = rename_function_ast(source, old_name, new_name)
    if renamed != source:
        return renamed
    # AST parse failed OR the name wasn't found — fall through to the regex
    pattern = re.compile(rf"^(\s*)(async\s+def|def)\s+{re.escape(old_name)}\b", re.MULTILINE)
    return pattern.sub(rf"\1\2 {new_name}", source, count=1)


class EquivalenceTestsPipeline:
    """R2E-style function-level equivalence-test synthesis."""

    name: ClassVar[PipelineName] = PipelineName.EQUIVALENCE_TESTS
    requires_bootstrap: ClassVar[bool] = True
    experimental: ClassVar[bool] = True
    supported_languages: ClassVar[frozenset[LanguageHint] | None] = frozenset({LanguageHint.PYTHON})

    def __init__(
        self,
        input: GenerationInput,
        options: EquivalenceTestsOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "equivalence_tests requires a BootstrapResult (set requires_bootstrap=True "
                "and let cmd_generate trigger it, or pass one explicitly)"
            )
        if input.llm is None:
            raise ValueError("equivalence_tests requires --llm (provider/model)")
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
        token = resolve_repo_token(self.input.repo, self.input.auth)
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

        with tempfile.TemporaryDirectory(prefix="r2e-equiv-tests-") as tmp:
            clone_dir = Path(tmp) / "repo"
            try:
                _shallow_clone_at_ref(
                    self.input.repo.url, self.input.repo.ref, token, clone_dir, depth=1
                )
            except Exception as exc:
                raise RuntimeError(f"failed to clone {self.input.repo.url}: {exc}") from exc

            candidates = list(
                walk_repo(
                    clone_dir,
                    file_glob=self.options.file_glob,
                    exclude_glob=self.options.exclude_glob,
                    min_loc=self.options.min_loc,
                    max_loc=self.options.max_loc,
                )
            )
            logger.info("equivalence_tests: %d candidate functions", len(candidates))
            rng.shuffle(candidates)
            seen_fingerprints: set[str] = set()

            try:
                if not self.options.skip_validation:
                    sandbox = self._start_sandbox()

                for cand in candidates:
                    if emitted >= self.options.limit:
                        break
                    candidates_seen += 1
                    label = f"{owner_name}:{cand.relative_path}::{cand.name}"

                    # Retry the LLM synth + gates + Stage-A/B verify up to N
                    # times per candidate, feeding back the failure signal on
                    # each retry. Was documented in options but never wired
                    # pre-v0.8.7 — the dominant baseline skip reason was
                    # `oracle_does_not_satisfy_test` (Stage-B fail on a
                    # first-draft LLM test), so verify HAS to be inside the
                    # loop, not outside.
                    parsed = None
                    parsed_fp: str | None = None
                    outcome: _VerifyOutcome | None = None
                    last_skip_reason = ""
                    feedback: str | None = None
                    for attempt in range(max(1, self.options.max_attempts_per_function)):
                        candidate_test = self._llm_generate_test(cand, feedback=feedback)
                        if candidate_test is None:
                            last_skip_reason = "llm_parse_failed"
                            feedback = None
                            continue
                        # Syntactic: both names referenced
                        if not uses_both_names(candidate_test.code, cand.name):
                            last_skip_reason = "test_missing_both_names"
                            feedback = (
                                "The previous attempt did not import both "
                                f"`{cand.name}` and `reference_{cand.name}` "
                                "from `task_module`, or did not reference both "
                                "identifiers in the test body. Both are required."
                            )
                            continue
                        ok, reason = check_equivalence_test_strength(
                            candidate_test.code, cand.name, min_test_cases=5
                        )
                        if not ok:
                            last_skip_reason = reason
                            feedback = (
                                f"The previous attempt failed the test-strength "
                                f"gate with reason `{reason}`. Ensure ≥5 distinct "
                                f"`def test_*` functions, each asserting "
                                f"`{cand.name}(...) == reference_{cand.name}(...)` "
                                f"on a genuinely different input. No `assert True`."
                            )
                            continue
                        fp = _equivalence_fingerprint(cand.name, candidate_test.code)
                        if fp in seen_fingerprints:
                            last_skip_reason = "duplicate_task"
                            feedback = (
                                "This exact test suite was already emitted for "
                                "another candidate. Pick a different set of inputs."
                            )
                            continue
                        test_filename = self._task_test_filename(cand, candidate_test)
                        if self.options.skip_validation:
                            parsed = candidate_test
                            parsed_fp = fp
                            break
                        # Stage-A / Stage-B verification against the sandbox.
                        outcome = self._verify_task(
                            sandbox=sandbox,
                            cand=cand,
                            test_code=candidate_test.code,
                            test_filename=test_filename,
                        )
                        if outcome.accepted:
                            parsed = candidate_test
                            parsed_fp = fp
                            logger.debug(
                                "candidate %s accepted on attempt %d",
                                label,
                                attempt + 1,
                            )
                            break
                        # Give the LLM enough of the log to see WHY it failed.
                        # Truncated so the retry prompt stays cheap.
                        log_hint = outcome.oracle_log or outcome.stub_log
                        if log_hint:
                            log_hint = log_hint[-1200:]
                        feedback = (
                            f"The previous attempt's test was rejected at "
                            f"`{outcome.reason}`. Failure log tail:\n\n"
                            f"```\n{log_hint or '(no log captured)'}\n```\n\n"
                            f"Pick simpler inputs that the reference actually "
                            f"handles (avoid inputs that depend on ambient click "
                            f"context, mutable global state, or randomness). "
                            f"If the function raises for some inputs, that's OK — "
                            f"assert `reference_{cand.name}(x) == {cand.name}(x)` "
                            f"is what we need, so use inputs where both sides "
                            f"return the same value or both raise the same "
                            f"exception."
                        )
                        last_skip_reason = outcome.reason

                    if parsed is None:
                        skip_reasons[last_skip_reason] = skip_reasons.get(last_skip_reason, 0) + 1
                        # Dump the last-attempt artifacts to a debug dir so we
                        # can inspect why Stage-B keeps failing without running
                        # the whole pipeline again. Cheap; disk-only.
                        try:
                            debug_dir = out_dir / ".debug_skips" / cand.name
                            debug_dir.mkdir(parents=True, exist_ok=True)
                            if candidate_test is not None:
                                (debug_dir / "last_test.py").write_text(candidate_test.code)
                            if outcome is not None:
                                (debug_dir / "stub_log.txt").write_text(outcome.stub_log or "")
                                (debug_dir / "oracle_log.txt").write_text(outcome.oracle_log or "")
                        except Exception:
                            pass
                        logger.info(
                            "skipped %s after %d attempts (reason=%s)",
                            label,
                            self.options.max_attempts_per_function,
                            last_skip_reason,
                        )
                        self._emit_progress(label, "skip", last_skip_reason)
                        continue

                    test_filename = self._task_test_filename(cand, parsed)
                    task = self._build_task(cand, parsed.code, test_filename=test_filename)
                    write_harbor_task(task, out_dir)
                    if parsed_fp is not None:
                        seen_fingerprints.add(parsed_fp)
                    emitted += 1
                    logger.info(
                        "emitted task %s (function=%s, loc=%d)",
                        task.name,
                        cand.name,
                        cand.body_loc,
                    )
                    self._emit_progress(task.name, "emit")
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

    # ----- LLM ---------------------------------------------------------------

    def _llm_generate_test(
        self, cand: FunctionCandidate, *, feedback: str | None = None
    ) -> _ParsedTest | None:
        user = PROMPT_USER_TEMPLATE.format(
            path=cand.relative_path,
            start=cand.lineno,
            end=cand.end_lineno,
            source=cand.source,
            arg_names=", ".join(cand.arg_names),
        )
        if feedback:
            user = (
                user
                + "\n\n"
                + "**Retry — this candidate failed the previous attempt. "
                + f"Adjust accordingly.**\n\n{feedback}"
            )
        try:
            resp = complete(
                self.input.llm,
                system=PROMPT_SYSTEM.format(name=cand.name),
                user=user,
                max_tokens=self.options.max_llm_tokens,
                temperature=self.options.llm_temperature,
            )
        except Exception as exc:
            logger.warning("equivalence_tests LLM call failed: %s", exc)
            return None
        self._llm_cost_usd += resp.cost_usd
        return _extract_test_section(resp.content)

    # ----- sandbox -----------------------------------------------------------

    def _start_sandbox(self):
        from repo2rlenv.bootstrap.docker import DockerSandbox

        marker = Path(tempfile.mkdtemp(prefix="r2e-equiv-tests-"))
        (marker / ".keep").write_text("")
        return DockerSandbox.start(
            base_image=self.bootstrap.image_tag,
            repo_dir=marker,
            platform=self.input.bootstrap.platform,
        )

    def _verify_task(
        self,
        *,
        sandbox,
        cand: FunctionCandidate,
        test_code: str,
        test_filename: str,
    ) -> _VerifyOutcome:
        """Two-stage verification.

        Stage A: stub `<name>`, keep `reference_<name>` correct → test must FAIL.
        Stage B: both `<name>` and `reference_<name>` are the original → test
                 must PASS.

        If either invariant breaks, the task is skipped.
        """
        stub_module = _stub_module(cand)
        oracle_module = _oracle_module(cand)

        # Pre-flight: the stub module must actually be importable. Post-
        # annotation-strip it usually is, but if the body still references
        # a repo-internal Name we haven't allowlisted, catch it here rather
        # than burning a full sandbox exec.
        if not is_module_importable(stub_module):
            return _VerifyOutcome(
                accepted=False,
                reason="stub_module_not_importable",
            )
        if not is_module_importable(oracle_module):
            return _VerifyOutcome(
                accepted=False,
                reason="oracle_module_not_importable",
            )

        enc_test = base64.b64encode(test_code.encode("utf-8")).decode("ascii")

        # Stage A — stubbed
        enc_stub = base64.b64encode(stub_module.encode("utf-8")).decode("ascii")
        script_a = (
            "set -uxo pipefail\n"
            "cd /workspace\n"
            "git config --global --add safe.directory /workspace\n"
            "git reset --hard HEAD\n"
            "git clean -fdx -e .venv -e venv -e __pycache__ || true\n"
            f"echo {enc_stub} | base64 -d > task_module.py\n"
            f"echo {enc_test} | base64 -d > {test_filename}\n"
            ": 'START_TEST_OUTPUT'\n"
            f"python -m pytest {test_filename} -v --no-header || true\n"
            ": 'END_TEST_OUTPUT'\n"
        )
        a = sandbox.exec(script_a, timeout=self.options.validation_timeout_sec)
        stub_log = a.stdout[-4000:] if a.stdout else ""
        stub_passes = all_tests_passed(stub_log)
        if self.options.require_test_fails_with_stub and stub_passes:
            return _VerifyOutcome(
                accepted=False,
                reason="test_passes_with_stub",
                stub_log=stub_log,
            )

        # Stage B — oracle in place
        enc_oracle = base64.b64encode(oracle_module.encode("utf-8")).decode("ascii")
        script_b = (
            "set -uxo pipefail\n"
            "cd /workspace\n"
            f"echo {enc_oracle} | base64 -d > task_module.py\n"
            ": 'START_TEST_OUTPUT'\n"
            f"python -m pytest {test_filename} -v --no-header\n"
            ": 'END_TEST_OUTPUT'\n"
            f"rm -f task_module.py {test_filename}\n"
        )
        b = sandbox.exec(script_b, timeout=self.options.validation_timeout_sec)
        oracle_log = b.stdout[-4000:] if b.stdout else ""
        oracle_passes = b.ok and all_tests_passed(oracle_log)
        if self.options.require_test_passes_with_oracle and not oracle_passes:
            return _VerifyOutcome(
                accepted=False,
                reason="oracle_does_not_satisfy_test",
                stub_log=stub_log,
                oracle_log=oracle_log,
            )

        return _VerifyOutcome(accepted=True, stub_log=stub_log, oracle_log=oracle_log)

    # ----- task builder -------------------------------------------------------

    def _task_test_filename(self, cand: FunctionCandidate, parsed: _ParsedTest) -> str:
        h = hashlib.sha256()
        h.update(cand.relative_path.encode())
        h.update(b"\0")
        h.update(cand.name.encode())
        h.update(b"\0")
        h.update(parsed.code.encode())
        return f"test_r2e_{h.hexdigest()[:10]}.py"

    def _build_task(
        self,
        cand: FunctionCandidate,
        test_code: str,
        *,
        test_filename: str,
    ) -> HarborTask:
        owner, name = self.input.repo.owner_name
        h = hashlib.sha256()
        h.update(cand.relative_path.encode())
        h.update(b"\0")
        h.update(cand.name.encode())
        task_id = f"{owner}__{name}-eqv-{h.hexdigest()[:8]}"

        # Every agent starts from the stub (reference_<name> present, <name>
        # stubbed), baked into the image. The gold patch is a stub→oracle
        # modify-diff that fills in <name>; the equivalence test ships under
        # tests/ (mounted at /tests for every agent) and is copied into
        # /workspace by the verifier — see issue #54.
        stub_module = _stub_module(cand)
        oracle_module = _oracle_module(cand)
        gold_diff = make_unified_diff(stub_module, oracle_module, "task_module.py")
        eval_script = build_binary_eval_script(
            [
                f"cp /tests/{test_filename} /workspace/{test_filename}",
                f"python -m pytest {test_filename} -v --no-header",
            ],
            language=self.bootstrap.language.value,
        )
        image_ref = (
            self.bootstrap.image_digest
            if self.bootstrap.pushed_to_registry
            else self.bootstrap.image_tag
        )
        dockerfile = build_equivalence_dockerfile(image_ref, stub_module)

        instruction = _build_instruction(cand)

        repo2env = {
            "pipeline": "equivalence_tests",
            "pipeline_version": "0.7.1",
            "repo": f"{owner}/{name}",
            "ref": self.input.repo.ref,
            "reference": (
                f"https://github.com/{owner}/{name}/blob/{self.input.repo.ref}/"
                f"{cand.relative_path}#L{cand.lineno}-L{cand.end_lineno}"
            ),
            "source_access": self.input.repo.access,
            "built_at": datetime.now(UTC).isoformat(),
            "synthesis_llm": self.input.llm.qualified_name,
            "reward_kinds": ["test_execution"],
            "equivalence_tests": {
                "function_name": cand.name,
                "source_path": cand.relative_path,
                "source_lineno": cand.lineno,
                "source_end_lineno": cand.end_lineno,
                "body_loc": cand.body_loc,
                "arg_names": list(cand.arg_names),
                "test_filename": test_filename,
                "bootstrap_image": self.bootstrap.image_digest,
                "llm_cost_usd": round(self._llm_cost_usd, 6),
            },
        }

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=f"Implement {cand.name} equivalently to the reference",
            instruction=instruction,
            oracle_diff=gold_diff,
            repo2env=repo2env,
            difficulty="medium",
            category="equivalence",
            keywords=[name, "equivalence_tests"],
            environment_dockerfile=dockerfile,
            test_script=eval_script,
            aux_files={f"tests/{test_filename}": test_code},
        )


def _build_instruction(cand: FunctionCandidate) -> str:
    """Leak-free instruction — signature + docstring only, never the body.

    Pre-v0.8.7 the emitted `instruction.md` embedded the full source of the
    function under `**Source:**`, so any solving agent could copy the
    reference implementation verbatim. Baseline audit surfaced this on
    every emitted task. v0.8.7 ships only what the RFC always claimed:
    signature + docstring (see `signature_only_source` in `_eval_script.py`).
    A hand-written fallback preserves the RFC-documented shape if the AST
    parse fails.
    """
    sig = signature_only_source(cand.source)
    if sig is None:
        # Fallback — at worst show only the header line and rely on the
        # docstring block below.
        header = f"def {cand.name}({', '.join(cand.arg_names)}):\n    ..."
        sig_block = f"```python\n{header}\n```"
    else:
        sig_block = f"```python\n{sig}\n```"
    docstring_block = f"\n\n**Docstring**\n\n> {cand.docstring}\n" if cand.docstring else ""
    return (
        f"# Implement `{cand.name}`\n\n"
        f"Implement the function `{cand.name}` in `/workspace/task_module.py` "
        f"so that it is behaviorally equivalent to `reference_{cand.name}` "
        f"(which is already provided in `task_module.py`).\n\n"
        f"You are given the function's **signature and docstring only** — "
        f"the reference implementation lives in `task_module.py` alongside a "
        f"stub of `{cand.name}` that currently raises `NotImplementedError`.\n\n"
        f"**Contract (from `{cand.relative_path}` lines {cand.lineno}-{cand.end_lineno}):**\n\n"
        f"{sig_block}{docstring_block}\n\n"
        f"## How the grading works\n\n"
        f"The test file `test_r2e_*.py` imports both `{cand.name}` and "
        f"`reference_{cand.name}` from `task_module` and asserts equality "
        f"across ≥5 inputs. Your implementation passes when every assertion "
        f"holds. You may inspect `reference_{cand.name}` in `task_module.py` "
        f"to understand the behaviour to match — that's the intended "
        f"solve-path — but do NOT change the `reference_` implementation."
    )


def _equivalence_fingerprint(name: str, test_code: str) -> str:
    """Dedup fingerprint for equivalence-test candidates.

    Combines the extracted function name with a hash of the normalized test
    body (whitespace-collapsed + lowercased). Catches the case where the
    LLM re-emits the exact same test suite for the same function on
    different retries, or where two candidate functions across repos
    happen to share a name and get the same LLM-generated test.
    """
    norm = re.sub(r"\s+", " ", test_code.strip().lower())
    sig = f"{name}\0{norm}"
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]
