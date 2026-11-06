"""Shared verifier-script + diff helpers for synthesis pipelines.

Used by `code_instruct` and `equivalence_tests` to build their `tests/test.sh`
(single-file grading in Harbor's separate verifier) and their gold patches.
"""

from __future__ import annotations

import ast
import base64
import difflib
import json
import re as _re
import shlex
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

from repo2rlenv.pipelines import _pr_runtime_verifier as _verifier
from repo2rlenv.pipelines._grading_time import UNMEASURED_RUN_SEC
from repo2rlenv.pipelines.pr_runtime import (
    _PYTHONS,
    _child_path,
    _finish_unless,
    launch_tests,
    run_verifier,
    separate_head,
    separate_verifier_files,
)


def make_unified_diff(old: str, new: str, path: str) -> str:
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


# ---------------------------------------------------------------------------
# Single-file submissions graded in a separate verifier
# (code_instruct + equivalence_tests)
# ---------------------------------------------------------------------------
# Only /workspace/task_module.py crosses from the agent's container; Harbor
# copies that one file over the image's /workspace, which stays as built. The
# hidden test runs through the same launcher as pr_runtime and reports through
# JUnit, read by root only after every process of the test user is gone.
SUBMISSION_FILE = "task_module.py"
SUBMISSION_ARTIFACT: list[dict[str, str]] = [{"source": f"/workspace/{SUBMISSION_FILE}"}]
JUNIT_REPORT = "/workspace/.r2e-junit.xml"
# Root-owned log directory for the launcher during validation (it refuses one
# the test user could read).
_VALIDATE_DIR = "/opt/r2e-validate"


def python_test_command(test_filename: str) -> str:
    """The pytest command used at validation and at grading, character for character."""
    return (
        f"python -m pytest {shlex.quote(test_filename)} -v --no-header "
        f"-p no:cacheprovider --junitxml={JUNIT_REPORT}"
    )


def build_single_file_eval_script(
    test_filename: str,
    *,
    language: str | None = None,
    run_timeout: int | None = None,
) -> str:
    """`tests/test.sh` for a single-file submission in Harbor's separate verifier.

    Reward 1.0 iff pytest exits 0 and its JUnit report holds exactly the test
    IDs the reference solution passed at generation (`tests/expected.json`),
    every one passed: a skipped, missing or extra test fails, so a report the
    submission wrote itself can't stand in for the real run. With
    `run_timeout`, a run past it scores 0 (see `_grading_time`).
    """
    copies = f'cp "$SCRIPT_DIR/{test_filename}" /workspace/{test_filename}\n'
    return (
        separate_head(language)
        + f'R2E_SUBMISSION="$("$R2E_PY" -B -I -S "$SCRIPT_DIR/grader_tools.py" check '
        f'--path /workspace/{SUBMISSION_FILE})"\n'
        "R2E_CHECK_RC=$?\n"
        + _finish_unless(
            "R2E_CHECK_RC", "ungraded", "submission_check_failed", "submission check failed"
        )
        + '[ "$R2E_SUBMISSION" = ok ] || r2e_finish graded "submission_$R2E_SUBMISSION" '
        "'submission not graded as submitted'\n"
        + copies
        + f"rm -f {JUNIT_REPORT}\n"
        # One test run.
        + launch_tests(
            python_test_command(test_filename),
            language,
            timeout=run_timeout or UNMEASURED_RUN_SEC,
        )
        + "tail -c 1048576 /logs/verifier/test_output.log 2>/dev/null\n"
        + run_verifier(
            f"--junit {JUNIT_REPORT} "
            '--expected "$SCRIPT_DIR/expected.json" --exit-code "$TEST_EXIT_CODE" '
            '--timed-out "$R2E_TIMED_OUT" --seconds /logs/verifier/run.seconds '
            "--out-dir /logs/verifier"
        )
        + "r2e_open_logs\n"
        "exit 0\n"
    )


def single_file_verifier_files(
    test_filename: str,
    test_code: str,
    expected: list[str],
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Everything test.sh reads from /tests for a single-file submission."""
    here = Path(__file__).parent
    return {
        f"tests/{test_filename}": test_code,
        "tests/verifier.py": (here / "_pr_runtime_verifier.py").read_text(encoding="utf-8"),
        "tests/test_results.py": (here.parent / "quality" / "test_results.py").read_text(
            encoding="utf-8"
        ),
        "tests/expected.json": json.dumps(sorted(expected), indent=2),
        **{f"tests/{name}": content for name, content in (extra or {}).items()},
        **separate_verifier_files(),
    }


@dataclass(slots=True)
class PythonTestRun:
    statuses: dict[str, str] = field(default_factory=dict)
    exit_code: int | None = None
    log: str = ""
    # (test command seconds, whole validation stage seconds), for grading limits.
    sample: tuple[float, float] | None = None

    @property
    def passed(self) -> bool:
        """Every test passed and pytest exited 0: what the verifier grades as 1."""
        return (
            self.exit_code == 0
            and bool(self.statuses)
            and all(status == "PASSED" for status in self.statuses.values())
        )


def junit_statuses(report: bytes, exit_code: int) -> dict[str, str]:
    """Statuses from a JUnit report, as the verifier reads them; {} if it is unusable."""
    try:
        results = _verifier._load_test_results()(
            report.decode("utf-8", "replace"), returncode=exit_code
        )
    except ValueError:
        return {}
    return {name: status.upper() for name, status in results.statuses.items()}


_JUNIT_RE = _re.compile(r"R2E_JUNIT_BEGIN\n([A-Za-z0-9+/=]*)\nR2E_JUNIT_END")
_EXIT_RE = _re.compile(r"^R2E_TEST_EXIT_CODE=(\d+)$", _re.MULTILINE)
_SECONDS_RE = _re.compile(r"^R2E_TEST_SECONDS=([0-9.]+)$", _re.MULTILINE)


# `docker exec bash -lc <script>` passes the script as one argument, and Linux
# caps a single argument at 128 KiB (MAX_ARG_STRLEN), so file contents and
# patches can't ride inside a validation script. They are uploaded first, in
# chunks well below the cap, into a root-only directory.
_STAGE_DIR = "/opt/r2e-stage"
_STAGE_CHUNK = 64 * 1024


def upload(sandbox, contents: dict[str, str]) -> dict[str, str]:
    """Upload each value into the sandbox, base64-encoded; return key -> staged path.

    Replaces anything staged before, so a validation stage uploads everything it
    needs (patches and files) in one call.
    """
    if not contents:
        return {}
    names = {key: f"{_STAGE_DIR}/{index}.b64" for index, key in enumerate(sorted(contents))}
    setup = sandbox.exec(
        f"rm -rf {_STAGE_DIR} && mkdir -p {_STAGE_DIR} && chmod 700 {_STAGE_DIR} && "
        + " && ".join(f": > {staged}" for staged in names.values()),
        timeout=60,
    )
    if not setup.ok:
        raise RuntimeError(f"could not prepare {_STAGE_DIR} in the sandbox")
    for key, staged in names.items():
        encoded = base64.b64encode(contents[key].encode("utf-8")).decode("ascii")
        for start in range(0, len(encoded), _STAGE_CHUNK):
            chunk = encoded[start : start + _STAGE_CHUNK]
            if not sandbox.exec(f"printf %s {chunk} >> {staged}", timeout=60).ok:
                raise RuntimeError(f"could not upload {key} to the sandbox")
    return names


def write_staged(staged: dict[str, str]) -> list[str]:
    """Shell lines that write staged files (key = destination path) in place."""
    return [f"base64 -d < {path} > {shlex.quote(dest)}" for dest, path in staged.items()]


def stage_files(sandbox, files: dict[str, str]) -> list[str]:
    """Upload `files` into the sandbox; return the shell lines that write them in place."""
    return write_staged(upload(sandbox, files))


def run_python_tests(
    sandbox,
    *,
    files: dict[str, str],
    test_filename: str,
    timeout: int,
    language: str | None = None,
    remove: tuple[str, ...] = (),
) -> PythonTestRun:
    """Run the hidden test the way the verifier does, in the validation sandbox.

    Writes `files` over a clean checkout, runs `python_test_command` through the
    grader launcher as the unprivileged user, and reads the JUnit report back.
    """
    launcher = Path(__file__).with_name("_grader_launcher.py").read_text(encoding="utf-8")
    candidates = " ".join(_PYTHONS)
    lines = [
        "set -uo pipefail",
        "cd /workspace",
        "git config --global --add safe.directory /workspace",
        "git reset --hard HEAD >/dev/null",
        "git clean -fdx -e .venv -e venv -e __pycache__ >/dev/null || true",
        f'R2E_PY=""; for p in {candidates}; do [ -x "$p" ] && {{ R2E_PY="$p"; break; }}; done',
        '[ -n "$R2E_PY" ] || exit 1',
        *(f"rm -f {shlex.quote(path)}" for path in remove),
        *stage_files(sandbox, files),
        f"rm -f {JUNIT_REPORT}",
        _child_path(language).rstrip("\n"),
        f"mkdir -p {_VALIDATE_DIR} && chown 0:0 {_VALIDATE_DIR} && chmod 700 {_VALIDATE_DIR}",
        f"cat > {_VALIDATE_DIR}/launcher.py <<'R2E_GRADER_LAUNCHER'\n{launcher}\nR2E_GRADER_LAUNCHER",
        f'R2E_TEST_EXIT="$("$R2E_PY" -B -I -S {_VALIDATE_DIR}/launcher.py '
        f"--log {_VALIDATE_DIR}/test_output.log --cwd /workspace "
        f'--path "$R2E_CHILD_PATH" --writable /workspace '
        f"--seconds-out {_VALIDATE_DIR}/seconds --purge -- "
        f'{shlex.quote(python_test_command(test_filename))})"',
        f"cat {_VALIDATE_DIR}/test_output.log",
        'echo "R2E_TEST_EXIT_CODE=${R2E_TEST_EXIT}"',
        f'echo "R2E_TEST_SECONDS=$(cat {_VALIDATE_DIR}/seconds 2>/dev/null)"',
        "echo R2E_JUNIT_BEGIN",
        f"[ -f {JUNIT_REPORT} ] && [ ! -L {JUNIT_REPORT} ] && base64 < {JUNIT_REPORT} | tr -d '\\n'",
        "echo",
        "echo R2E_JUNIT_END",
    ]
    started = _time.monotonic()
    result = sandbox.exec("\n".join(lines), timeout=timeout)
    stage = _time.monotonic() - started
    output = result.stdout or ""
    run = PythonTestRun(log=output[-4000:])
    seconds = _SECONDS_RE.search(output)
    if seconds:
        run.sample = (float(seconds.group(1)), stage)
    exit_code = _EXIT_RE.search(output)
    run.exit_code = int(exit_code.group(1)) if exit_code else None
    report = _JUNIT_RE.search(output)
    if report and report.group(1) and run.exit_code is not None:
        run.statuses = junit_statuses(base64.b64decode(report.group(1)), run.exit_code)
    return run


def record_reference_calls(
    sandbox,
    *,
    files: dict[str, str],
    test_filename: str,
    record_file: str,
    hash_seed: int,
    timeout: int,
) -> list | None:
    """Run the recording test once, as the unprivileged grader user, and return what it recorded.

    Generation only: `files` hold the original implementation and a test that
    records each reference call into `record_file`. The test runs through the
    grader launcher like every other validation run; root only reads the
    recording back as data. None when the run produced no readable recording.
    """
    launcher = Path(__file__).with_name("_grader_launcher.py").read_text(encoding="utf-8")
    candidates = " ".join(_PYTHONS)
    command = (
        f"PYTHONHASHSEED={int(hash_seed)} python -m pytest {shlex.quote(test_filename)} "
        "-q --no-header -p no:cacheprovider"
    )
    lines = [
        "set -uo pipefail",
        "cd /workspace",
        "git config --global --add safe.directory /workspace",
        "git reset --hard HEAD >/dev/null",
        "git clean -fdx -e .venv -e venv -e __pycache__ >/dev/null || true",
        f'R2E_PY=""; for p in {candidates}; do [ -x "$p" ] && {{ R2E_PY="$p"; break; }}; done',
        '[ -n "$R2E_PY" ] || exit 1',
        *stage_files(sandbox, files),
        f"rm -f {shlex.quote(record_file)}",
        _child_path("python").rstrip("\n"),
        f"mkdir -p {_VALIDATE_DIR} && chown 0:0 {_VALIDATE_DIR} && chmod 700 {_VALIDATE_DIR}",
        f"cat > {_VALIDATE_DIR}/launcher.py <<'R2E_GRADER_LAUNCHER'\n{launcher}\nR2E_GRADER_LAUNCHER",
        f'"$R2E_PY" -B -I -S {_VALIDATE_DIR}/launcher.py '
        f"--log {_VALIDATE_DIR}/record_output.log --cwd /workspace "
        f'--path "$R2E_CHILD_PATH" --writable /workspace --purge -- '
        f"{shlex.quote(command)} >/dev/null",
        "echo R2E_RECORD_BEGIN",
        f"[ -f {shlex.quote(record_file)} ] && [ ! -L {shlex.quote(record_file)} ] && "
        f"base64 < {shlex.quote(record_file)} | tr -d '\\n'"
        " || printf W10=",  # an empty list: the test never called the reference
        "echo",
        "echo R2E_RECORD_END",
        f"rm -f {shlex.quote(record_file)}",
    ]
    result = sandbox.exec("\n".join(lines), timeout=timeout)
    found = _RECORD_RE.search(result.stdout or "")
    if not found:
        return None
    try:
        recorded = json.loads(base64.b64decode(found.group(1)))
    except (ValueError, UnicodeDecodeError):
        return None
    return recorded if isinstance(recorded, list) else None


_RECORD_RE = _re.compile(r"R2E_RECORD_BEGIN\n([A-Za-z0-9+/=]*)\nR2E_RECORD_END")


# ---------------------------------------------------------------------------
# Function-signature extraction (for anti-leak instructions)
# ---------------------------------------------------------------------------


def signature_only_source(source: str) -> str | None:
    """Extract a signature + docstring + `...` body from a full function source.

    Used by `equivalence_tests` to build a leak-free instruction that shows
    the solving agent the function's contract but NOT its body. Pre-v0.8.7
    the whole source was embedded, making the task trivially copyable.

    Returns None on parse failure — the caller should fall back to a
    hand-written header line.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef | ast.AsyncFunctionDef):
        return None
    fn = tree.body[0]
    # Rebuild the def line by unparsing the args + decorators
    decorators = "".join(f"@{ast.unparse(d)}\n" for d in fn.decorator_list)
    async_kw = "async " if isinstance(fn, ast.AsyncFunctionDef) else ""
    args = ast.unparse(fn.args)
    returns = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
    docstring = ast.get_docstring(fn)
    body_lines: list[str] = []
    if docstring:
        # Preserve the docstring's original quotes as best we can
        body_lines.append(f'    """{docstring}"""')
    body_lines.append("    ...")
    return f"{decorators}{async_kw}def {fn.name}({args}){returns}:\n" + "\n".join(body_lines)


# ---------------------------------------------------------------------------
# AST-based function rename (recursion-safe)
# ---------------------------------------------------------------------------


class _NameRenamer(ast.NodeTransformer):
    def __init__(self, old_name: str, new_name: str):
        self._old = old_name
        self._new = new_name

    def visit_FunctionDef(self, node):
        if node.name == self._old:
            node.name = self._new
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node):
        if node.name == self._old:
            node.name = self._new
        self.generic_visit(node)
        return node

    def visit_Call(self, node):
        # Rewrite recursive calls in the body
        if isinstance(node.func, ast.Name) and node.func.id == self._old:
            node.func = ast.copy_location(ast.Name(id=self._new, ctx=ast.Load()), node.func)
        self.generic_visit(node)
        return node

    def visit_Name(self, node):
        # Also catch bare references (e.g. `factorial = factorial`) that
        # would otherwise leave the old symbol dangling.
        if node.id == self._old and isinstance(node.ctx, ast.Load):
            node.id = self._new
        return node


def rename_function_ast(source: str, old_name: str, new_name: str) -> str:
    """AST-based rewrite of `def OLD(...)` and all references to OLD → NEW.

    Recursion-safe: unlike a regex-on-the-def-line, this also rewrites
    recursive calls (`OLD(x-1)` inside the body) and bare Name loads,
    so a renamed reference oracle actually recurses on itself.

    Falls back to the source unchanged if AST parsing fails — the caller
    should treat that as a soft signal and skip the candidate.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    renamer = _NameRenamer(old_name, new_name)
    new_tree = renamer.visit(tree)
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree)


class _AnnotationStripper(ast.NodeTransformer):
    """Strip type annotations from function defs so the stub imports cleanly.

    Signature-only external references (`def foo(x: Argument) -> FC`) are
    the dominant `oracle_does_not_satisfy_test` cause in equivalence_tests
    v0.7 — the annotation types don't exist in the standalone
    `task_module.py` we bake, so import fails before pytest even starts.
    Stripping annotations is safe: Python doesn't evaluate them at runtime
    (they're stored as strings/objects on `__annotations__`) but their
    presence in the def line DOES trigger a NameError at import time.
    """

    def _clean_args(self, args: ast.arguments) -> ast.arguments:
        for a in args.posonlyargs + args.args + args.kwonlyargs:
            a.annotation = None
        if args.vararg is not None:
            args.vararg.annotation = None
        if args.kwarg is not None:
            args.kwarg.annotation = None
        return args

    def visit_FunctionDef(self, node):
        node.args = self._clean_args(node.args)
        node.returns = None
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node):
        node.args = self._clean_args(node.args)
        node.returns = None
        self.generic_visit(node)
        return node

    def visit_AnnAssign(self, node):
        # Convert `x: T = v` → `x = v`, drop `x: T` (no value) entirely.
        if node.value is None:
            return None
        new_node = ast.Assign(
            targets=[node.target],
            value=node.value,
            type_comment=None,
        )
        return ast.copy_location(new_node, node)


def strip_annotations(source: str) -> str:
    """Return the source with type annotations removed. Falls back to the
    input verbatim on parse failure.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    tree = _AnnotationStripper().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def is_module_importable(source: str) -> bool:
    """True iff `source` compiles AND all top-level Names can be resolved to
    Python builtins (i.e., the module would import without a NameError).

    Cheap post-strip smoke test — parses + compiles + resolves top-level
    Name loads against `builtins`. Doesn't execute any code, so it's safe.
    """
    import builtins

    try:
        tree = ast.parse(source)
        compile(tree, "<stub>", "exec")
    except SyntaxError:
        return False
    known: set[str] = set(dir(builtins))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            known.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                for n in ast.walk(tgt):
                    if isinstance(n, ast.Name):
                        known.add(n.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            known.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                known.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                known.add(alias.asname or alias.name)
    # Top-level Name loads (module-level default values, class-body statements)
    for node in tree.body:
        for sub in ast.iter_child_nodes(node):
            for name_node in ast.walk(sub):
                if isinstance(name_node, ast.Name) and isinstance(name_node.ctx, ast.Load):
                    if name_node.id in known:
                        continue
                    # Function bodies are lazy — Names inside them don't need
                    # to resolve at import time. Only check module-level
                    # default expressions.
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                        # But default arg values ARE evaluated at def time
                        if sub is getattr(node, "args", None):
                            for d in node.args.defaults + node.args.kw_defaults:
                                if d is None:
                                    continue
                                for n in ast.walk(d):
                                    if (
                                        isinstance(n, ast.Name)
                                        and isinstance(n.ctx, ast.Load)
                                        and n.id not in known
                                    ):
                                        return False
                        continue
                    return False
    return True
