"""AST function extractor for `equivalence_tests`.

Walks a parsed module and yields `FunctionCandidate` records for every
module-level function that survives R2E-style filters. The pipeline picks
candidates and asks the LLM to write equivalence tests against each.

v0.7 scope: module-level functions only. Class methods (with `self` / `cls`)
and class hierarchies are deferred — they need either dependency slicing
or class-context inlining to be self-contained.

Acknowledgment
--------------
Filter set mirrors the spirit of R2E's `src/r2e/repo_builder/fut_extractor/`
(LOC bounds, must have docstring, must have explicit return, exclude
test/main/demo names). Implementation is original Python stdlib.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class FunctionCandidate:
    """One module-level function picked as an equivalence-test seed."""

    relative_path: str  # POSIX, e.g. "src/foo/bar.py"
    name: str  # function name
    lineno: int  # 1-indexed start
    end_lineno: int  # 1-indexed end (inclusive)
    body_loc: int  # # lines in the function body (signature excluded)
    source: str  # the full `def ...: ...` text from the file
    docstring: str  # extracted via ast.get_docstring (empty if none)
    arg_names: tuple[str, ...]  # positional + keyword arg names, in order


# Names that are almost never useful task candidates.
_SKIP_NAME_PREFIXES = ("_",)
_SKIP_EXACT_NAMES = {"main", "setup", "run", "init", "cli", "wrapper"}

# Patterns inside the body that suggest the function isn't behaviorally pure
# enough for equivalence testing in a standalone module.
_BODY_SIDE_EFFECT_PATTERNS = (
    "open(",
    "subprocess.",
    "os.system",
    "os.environ",
    "os.remove",
    "os.rename",
    "shutil.",
    "requests.",
    "http.",
    "urllib.",
    "socket.",
    "logging.",
    "print(",
    "sys.exit",
    "input(",
)


def _is_excluded(relative_path: str, exclude_globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(relative_path, pat) for pat in exclude_globs)


def _function_body_loc(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """# lines in the function body (signature/decorators excluded).

    Uses end_lineno - body[0].lineno + 1 so we don't count the `def` line.
    Falls back to 0 if line numbers aren't populated (synthetic AST).
    """
    if not node.body:
        return 0
    first = node.body[0]
    start = getattr(first, "lineno", 0)
    end = getattr(node, "end_lineno", 0)
    if not (start and end):
        return 0
    return max(0, end - start + 1)


def _function_source(source_lines: list[str], node: ast.FunctionDef) -> str:
    """Slice the full `def <name>(...): ...` text out of the source.

    Includes decorators. Uses ast's lineno/end_lineno (1-indexed).
    """
    decorator_lines = [getattr(d, "lineno", node.lineno) for d in node.decorator_list]
    start = min([node.lineno, *decorator_lines]) - 1
    end = getattr(node, "end_lineno", node.lineno)
    return "\n".join(source_lines[start:end])


def _arg_names(node: ast.FunctionDef) -> tuple[str, ...]:
    """Positional + keyword arg names (no *args / **kwargs)."""
    args = node.args
    out: list[str] = []
    for a in args.posonlyargs + args.args + args.kwonlyargs:
        out.append(a.arg)
    return tuple(out)


def _has_explicit_return(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True iff the function body contains a `return <expr>` (not bare `return`)."""
    for sub in ast.walk(node):
        if sub is node:
            continue
        # Don't descend into nested function/class definitions
        if isinstance(sub, ast.Return) and sub.value is not None:
            return True
    return False


def _body_has_side_effect(source: str) -> bool:
    """Heuristic: function body contains a substring suggesting side effects."""
    return any(pat in source for pat in _BODY_SIDE_EFFECT_PATTERNS)


def _is_skipped_name(name: str) -> bool:
    if name in _SKIP_EXACT_NAMES:
        return True
    if name.startswith("test_"):
        return True
    return any(name.startswith(prefix) for prefix in _SKIP_NAME_PREFIXES)


def extract_from_module(
    path: Path,
    source: str,
    *,
    relative_path: str,
    min_loc: int,
    max_loc: int,
) -> list[FunctionCandidate]:
    """Parse `source` and return every module-level function that survives filters.

    Returns an empty list on SyntaxError.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    out: list[FunctionCandidate] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # Async functions can't be tested with plain pytest assertions; skip
        if isinstance(node, ast.AsyncFunctionDef):
            continue
        # Filter: name
        if _is_skipped_name(node.name):
            continue
        # Filter: argument count (no zero-arg, no *args/**kwargs only)
        names = _arg_names(node)
        if not names:
            continue
        # Filter: body size
        body_loc = _function_body_loc(node)
        if body_loc < min_loc or body_loc > max_loc:
            continue
        # Filter: explicit return
        if not _has_explicit_return(node):
            continue
        # Filter: side-effect heuristics on the source
        try:
            src = _function_source(source_lines, node)
        except IndexError:
            continue
        if _body_has_side_effect(src):
            continue

        out.append(
            FunctionCandidate(
                relative_path=relative_path,
                name=node.name,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", node.lineno),
                body_loc=body_loc,
                source=src,
                docstring=ast.get_docstring(node) or "",
                arg_names=names,
            )
        )
    return out


def walk_repo(
    clone_dir: Path,
    *,
    file_glob: str,
    exclude_glob: list[str],
    min_loc: int,
    max_loc: int,
) -> Iterator[FunctionCandidate]:
    """Walk `clone_dir`, yield FunctionCandidate from every matching .py file."""
    for path in clone_dir.glob(file_glob):
        if not path.is_file():
            continue
        rel = str(path.relative_to(clone_dir))
        if _is_excluded(rel, exclude_glob):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        yield from extract_from_module(
            path, text, relative_path=rel, min_loc=min_loc, max_loc=max_loc
        )
