"""Shared verifier-script + diff helpers for synthesis pipelines.

These were originally defined in `mutation_bugs.py`; they outlived that
pipeline and are used by `code_instruct` and `equivalence_tests` to build
their `tests/test.sh` (binary pass/fail reward) and their gold patches.
"""

from __future__ import annotations

import ast
import difflib
import re


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

    Moved here (from `pr_runtime.py`) so both `pr_runtime` and
    `_eval_script`'s own `build_binary_eval_script` can use it without a
    `pr_runtime → _eval_script → pr_runtime` import cycle. `pr_runtime`
    re-exports this name — do not remove the export.
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


def normalize_test_cmds_for_runtime(test_cmds: list[str]) -> list[str]:
    """Adapt bootstrap-recorded test commands for actual per-PR execution.

    Bootstrap prefers fast/tolerant commands (e.g. `pytest --collect-only`)
    so it can declare success without running every test. For pr_runtime,
    we need commands that *run* tests and emit per-test pass/fail lines
    that our parsers can read.

    Transforms (per runner):
      pytest:
        - Drop `--collect-only` / `--co` so pytest actually runs tests
        - Drop `-q` / `--quiet`: suppresses per-test names; cancels `-v` in pytest 9
        - Add `-v` if no verbosity flag is present
      go test:
        - Add `-v` if missing (default `go test` doesn't print --- PASS lines)
      cargo test:
        - Default output is already parseable; no transform needed
      jest / npm test:
        - Add `--verbose` if not present, so per-test ✓/✕ lines are emitted
        - Some configs swallow stdout via `--silent`; we strip that

    Commands that normalize to the empty string are DROPPED, not emitted.
    The pipe/redirect strippers below run before runner detection, so a
    degenerate bootstrap-recorded entry (`"| head -50"`, `"2>&1"`, `"   "`)
    reduces to `""`. Callers join the result with `" && "`, and an empty
    segment is a bash syntax error rather than a no-op. The output is
    therefore NOT index-aligned with the input; no caller relies on that
    (every one pipes straight into `targeted_test_cmds_for_pr`).

    Moved here (from `pr_runtime.py`) as part of the `_eval_script.py`
    consolidation. `pr_runtime` re-exports this name — `cve_patches`,
    `commit_runtime`, `pr_to_env`, and `tests/test_pipeline_pr_runtime.py`
    all import it from `pr_runtime`, not from here.
    """
    out: list[str] = []
    for cmd in test_cmds:
        cleaned = cmd

        # Strip shell pipes / redirects / tail-truncators that bootstrap agents
        # sometimes append (e.g. `pytest -q 2>&1 | head -50`) so we capture only
        # the test runner invocation. If we keep them, `targeted_test_cmds_for_pr`
        # appends test files AFTER the pipe → broken command.
        # `[^|]*` swallows whatever flags follow `head`/`tail` (`-50`, `-n 100`, etc.)
        # without crossing into another piped command.
        cleaned = re.sub(r"\s*\|\s*(?:head|tail)\s*[^|]*$", "", cleaned)
        cleaned = re.sub(r"\s*2>&1\b", "", cleaned)
        cleaned = re.sub(r"\s*&?>\s*/dev/null\b", "", cleaned)
        cleaned = cleaned.rstrip(" |&")

        # Nothing but shell plumbing (or whitespace) survived the strip — this
        # entry was never a test invocation. Drop it; emitting "" would inject
        # an empty segment into the downstream `" && ".join(...)`.
        if not cleaned.strip():
            continue

        # --- pytest ---
        if re.search(r"\bpytest\b", cleaned):
            cleaned = re.sub(r"\s+--collect-only\b", "", cleaned)
            cleaned = re.sub(r"\s+--co\b", "", cleaned)  # pytest's short form
            # Strip -q/--quiet: it suppresses per-test names that the log parser needs.
            # -q and -v cancel each other in pytest 9 (verbosity counter), so -q must go.
            cleaned = re.sub(r"\s+(?:-q|--quiet)\b", "", cleaned)
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

        stripped = cleaned.strip()
        if stripped:
            out.append(stripped)
    return out


# Leading env-setup fragment: `. <path>`, `source <path>`, or `export FOO=...`.
_ENV_FRAGMENT_RE = re.compile(r"^(?:\.\s+\S|source\s+\S|export\s+\w+=)")


def env_prelude_from_test_cmds(test_cmds: list[str]) -> str:
    """Extract the leading environment-setup fragments from `test_cmds`.

    Bootstrap-recorded `test_cmds` often carry a venv-activation / export
    prefix (`. /workspace/.venv/bin/activate && pytest -v`) that the real
    test invocation depends on. This pulls out just those leading fragments
    — `. <path>/activate`, `source ...`, `export FOO=bar` — so they can be
    shipped as a standalone, *sourced* file (`tests/env_prelude.sh`) rather
    than re-interpolated into a shell string (which would break on a
    fragment containing a `'`, e.g. `export PYTEST_ADDOPTS='-p no:randomly'`).

    Each fragment has its trailing `&&`/`;` stripped. Stops at the first
    segment in a command that ISN'T an env-setup fragment (the actual test
    invocation) — everything after that point is irrelevant here. Returns
    the literal string `"true"` (a shell no-op) when no fragments are found
    across any command, so the emitted file is always safe to `source`.
    """
    seen: set[str] = set()
    fragments: list[str] = []
    for cmd in test_cmds:
        for part in re.split(r"\s*(?:&&|;)\s*", cmd):
            part = part.strip()
            if not part:
                continue
            if not _ENV_FRAGMENT_RE.match(part):
                break
            if part not in seen:
                seen.add(part)
                fragments.append(part)
    if not fragments:
        return "true"
    return "\n".join(fragments)


# host → (build-arg-injected username, host) for authed_clone_url.
_CLONE_HOST_CREDS: tuple[tuple[str, str], ...] = (
    ("https://github.com/", "x-access-token"),
    ("https://gitlab.com/", "oauth2"),
)


def authed_clone_url(repo_url: str, *, arg_name: str = "GITHUB_TOKEN") -> str:
    """Build a clone URL with a build-arg token injected after the scheme.

    `github.com` → `https://x-access-token:${<arg_name>}@github.com/...`
    `gitlab.com` → `https://oauth2:${<arg_name>}@gitlab.com/...`

    Returns `repo_url` unchanged if it matches neither known host (same
    no-op fallback as the hardcoded `.replace(...)` this consolidates).
    """
    for prefix, username in _CLONE_HOST_CREDS:
        if repo_url.startswith(prefix):
            return repo_url.replace(prefix, f"https://{username}:${{{arg_name}}}@{prefix[8:]}", 1)
    return repo_url


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
    if "failed" in lower and re.search(r"\b[1-9]\d*\s+failed\b", lower):
        return False
    return bool(re.search(r"\b[1-9]\d*\s+passed\b", lower))


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
