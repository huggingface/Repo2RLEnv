"""Shared verifier-script + diff helpers for synthesis pipelines.

These were originally defined in `mutation_bugs.py`; they outlived that
pipeline and are used by `code_instruct` and `equivalence_tests` to build
their `tests/test.sh` (binary pass/fail reward) and their gold patches.
"""

from __future__ import annotations

import ast
import difflib
import re as _re

from repo2rlenv.pipelines.pr_runtime import _path_prelude_for_language


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


def build_binary_eval_script(test_cmds: list[str], *, language: str | None = None) -> str:
    """Build a `tests/test.sh` that maps test exit code to a binary reward.

    Runs the commands wrapped in START/END markers and writes 1.0/0.0 to
    /logs/verifier/reward.txt (1.0 iff the test command exits 0).
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


# ---------------------------------------------------------------------------
# Log-parse heuristic (shared by code_instruct + equivalence_tests)
# ---------------------------------------------------------------------------


def all_tests_passed(log: str) -> bool:
    """Heuristic: pytest summary line ends with `N passed` and no `failed`/`error`.

    Used by synthesis pipelines that run pytest inside the sandbox with a
    `|| true` wrapper — pytest's exit code is masked, so we scrape the log
    to decide pass/fail. Moved here from `code_instruct.py` so both
    `code_instruct` and `equivalence_tests` can import from one place.
    """
    lower = log.lower()
    if "error" in lower and "collected 0 items" in lower:
        return False
    if "failed" in lower and _re.search(r"\b[1-9]\d*\s+failed\b", lower):
        return False
    return bool(_re.search(r"\b[1-9]\d*\s+passed\b", lower))


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
