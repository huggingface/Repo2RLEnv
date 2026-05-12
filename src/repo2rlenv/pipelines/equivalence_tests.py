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

from repo2rlenv.auth import resolve_github_token
from repo2rlenv.bootstrap.runner import _shallow_clone_at_ref
from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.llm import complete
from repo2rlenv.pipelines._function_extractor import FunctionCandidate, walk_repo
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.code_instruct import _all_tests_passed, build_code_instruct_dockerfile
from repo2rlenv.pipelines.mutation_bugs import build_mutation_eval_script
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import EquivalenceTestsOptions

logger = logging.getLogger(__name__)


PROMPT_SYSTEM = """You are a senior Python engineer writing differential equivalence tests.

You will be shown one function from an open-source library. Your job: write a single pytest test file that imports BOTH `<name>` and `reference_<name>` from the module `task_module`, and asserts they produce identical outputs across a diverse set of inputs.

STRICT REQUIREMENTS:
- Output ONE section labelled `[Test]` and nothing else (no preamble, no closing notes).
- Use plain `def test_*(): assert ...` style — NO unittest.TestCase, NO fixtures, NO mocks.
- The test MUST import both names from `task_module`: `from task_module import {name}, reference_{name}`.
- Cover at least 5 distinct inputs: normal cases + edge cases (empty / zero / boundary) + adversarial cases where it makes sense.
- Use literal Python values that can be constructed without third-party libraries. If the function takes complex types, build them in the test.
- Each input case should be its own `def test_*():` function (so failures are isolated).
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

    Used in Stage A of verification: confirms the test actually exercises
    `<name>` (else it would pass when `<name>` raises NotImplementedError).
    """
    ref_source = _rename_function_source(
        candidate.source, candidate.name, f"reference_{candidate.name}"
    )
    args = ", ".join(candidate.arg_names) if candidate.arg_names else "*args, **kwargs"
    stub = (
        f"def {candidate.name}({args}):\n"
        f'    raise NotImplementedError("implement {candidate.name}")\n'
    )
    return ref_source + "\n\n" + stub


def _oracle_module(candidate: FunctionCandidate) -> str:
    """Build `task_module.py` with both `<name>` AND `reference_<name>` set to the original.

    Used in Stage B of verification AND as the gold-patch payload: the
    agent's job is to make `<name>` equivalent to `reference_<name>`;
    the gold patch achieves that by giving them the same implementation.
    """
    ref_source = _rename_function_source(
        candidate.source, candidate.name, f"reference_{candidate.name}"
    )
    return ref_source + "\n\n" + candidate.source + "\n"


def _rename_function_source(source: str, old_name: str, new_name: str) -> str:
    """Rename `def OLD(...)` → `def NEW(...)` in the source text.

    Only rewrites the `def` line itself, not internal recursive calls
    (those would change semantics if the function recurses by name).
    For v0.7 we accept this: most extractable functions aren't recursive,
    and the few that are will be skipped at Stage B (recursion still calls
    the *renamed* function so the reference call diverges from the
    candidate). The skip rate is low; deferring proper recursion handling
    to v0.8.
    """
    pattern = re.compile(rf"^(\s*)(async\s+def|def)\s+{re.escape(old_name)}\b", re.MULTILINE)
    return pattern.sub(rf"\1\2 {new_name}", source, count=1)


def _make_two_file_diff(*, module_code: str, test_code: str, test_filename: str) -> str:
    """Build a `git apply`-compatible diff adding `task_module.py` + a test file.

    Lifted from code_instruct.py — same shape (two new files at repo root).
    """

    def new_file_hunk(content: str, path: str) -> str:
        lines = content.splitlines()
        n = len(lines)
        header = (
            f"diff --git a/{path} b/{path}\n"
            f"new file mode 100644\n"
            f"index 0000000..0000001\n"
            f"--- /dev/null\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{n} @@\n"
        )
        body = "".join(f"+{ln}\n" for ln in lines)
        if not content.endswith("\n"):
            body += "\\ No newline at end of file\n"
        return header + body

    return new_file_hunk(module_code, "task_module.py") + new_file_hunk(test_code, test_filename)


class EquivalenceTestsPipeline:
    """R2E-style function-level equivalence-test synthesis."""

    name: ClassVar[PipelineName] = PipelineName.EQUIVALENCE_TESTS
    requires_bootstrap: ClassVar[bool] = True
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

            try:
                if not self.options.skip_validation:
                    sandbox = self._start_sandbox()

                for cand in candidates:
                    if emitted >= self.options.limit:
                        break
                    candidates_seen += 1
                    label = f"{owner_name}:{cand.relative_path}::{cand.name}"

                    parsed = self._llm_generate_test(cand)
                    if parsed is None:
                        skip_reasons["llm_parse_failed"] = (
                            skip_reasons.get("llm_parse_failed", 0) + 1
                        )
                        self._emit_progress(label, "skip", "llm_parse_failed")
                        continue

                    # Syntactic: test imports + uses both names
                    if not uses_both_names(parsed.code, cand.name):
                        skip_reasons["test_missing_both_names"] = (
                            skip_reasons.get("test_missing_both_names", 0) + 1
                        )
                        self._emit_progress(label, "skip", "test_missing_both_names")
                        continue

                    test_filename = self._task_test_filename(cand, parsed)

                    if not self.options.skip_validation:
                        outcome = self._verify_task(
                            sandbox=sandbox,
                            cand=cand,
                            test_code=parsed.code,
                            test_filename=test_filename,
                        )
                        if not outcome.accepted:
                            skip_reasons[outcome.reason] = skip_reasons.get(outcome.reason, 0) + 1
                            self._emit_progress(label, "skip", outcome.reason)
                            continue

                    task = self._build_task(cand, parsed.code, test_filename=test_filename)
                    write_harbor_task(task, out_dir)
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

    def _llm_generate_test(self, cand: FunctionCandidate) -> _ParsedTest | None:
        user = PROMPT_USER_TEMPLATE.format(
            path=cand.relative_path,
            start=cand.lineno,
            end=cand.end_lineno,
            source=cand.source,
            arg_names=", ".join(cand.arg_names),
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
        stub_passes = _all_tests_passed(stub_log)
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
        oracle_passes = b.ok and _all_tests_passed(oracle_log)
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

        module_code = _oracle_module(cand)
        gold_diff = _make_two_file_diff(
            module_code=module_code, test_code=test_code, test_filename=test_filename
        )
        eval_script = build_mutation_eval_script(
            [f"python -m pytest {test_filename} -v --no-header"],
            language=self.bootstrap.language.value,
        )
        image_ref = (
            self.bootstrap.image_digest
            if self.bootstrap.pushed_to_registry
            else self.bootstrap.image_tag
        )
        dockerfile = build_code_instruct_dockerfile(image_ref)

        instruction = _build_instruction(cand)

        repo2env = {
            "pipeline": "equivalence_tests",
            "pipeline_version": "0.7.0",
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
        )


def _build_instruction(cand: FunctionCandidate) -> str:
    docstring_block = f"\n\n**Docstring:**\n\n> {cand.docstring}\n" if cand.docstring else ""
    return (
        f"# Implement `{cand.name}`\n\n"
        f"Implement the function `{cand.name}` in `task_module.py` so that it is "
        f"behaviorally equivalent to `reference_{cand.name}` (which is already provided "
        f"in `task_module.py`).\n\n"
        f"**Source (from `{cand.relative_path}` lines {cand.lineno}-{cand.end_lineno}):**\n\n"
        f"```python\n{cand.source}\n```{docstring_block}\n\n"
        f"## Task\n\n"
        f"The test file `test_r2e_*.py` imports both `{cand.name}` and "
        f"`reference_{cand.name}` from `task_module` and asserts equality across "
        f"a variety of inputs. Your implementation passes when all assertions hold."
    )
