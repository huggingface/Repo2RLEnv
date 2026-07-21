"""Helpers for the `code_instruct` pipeline: sampling, parsing, decontam.

Magicoder OSS-Instruct is a recipe (prompt template + sampling + parsing),
not a reusable library. This module is the recipe.

Acknowledgment
--------------
Algorithms inspired by Magicoder
(`references/magicoder/src/magicoder/generate_data.py:79-102` and
`magicoder/decontamination/find_substrings.py`). No code copied; we
reimplement against Python stdlib only.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import logging
import random
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo package detection — the LLM's oracle must import from THIS package
# ---------------------------------------------------------------------------


def detect_repo_package(clone_dir: Path, repo_name: str) -> str | None:
    """Find the top-level Python package name of the target repo.

    Order of attempts:
      1. `pyproject.toml` → `[project.name]` (normalized to underscores)
         or `[tool.poetry.name]`.
      2. `src/<candidate>/__init__.py` for candidate in {repo_name,
         repo_name.replace('-', '_'), 'attr'} (attrs ships as `attr`).
      3. `<candidate>/__init__.py` at repo root, same candidates.

    Returns the package name (a valid Python identifier), or None if
    detection fails — callers should skip repo-anchoring enforcement
    in that case rather than block generation.
    """
    # Try pyproject
    py = clone_dir / "pyproject.toml"
    if py.is_file():
        try:
            data = tomllib.loads(py.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        name = (data.get("project", {}).get("name")) or (
            data.get("tool", {}).get("poetry", {}).get("name")
        )
        if name:
            candidate = name.replace("-", "_").lower()
            # Confirm the folder actually exists
            for base in (clone_dir / "src" / candidate, clone_dir / candidate):
                if (base / "__init__.py").is_file():
                    return candidate
    # Try well-known conventions
    candidates: list[str] = [
        repo_name.replace("-", "_").lower(),
        repo_name.lower(),
    ]
    # attrs is packaged as `attr`
    if repo_name.lower() in ("attrs",):
        candidates.insert(0, "attr")
    for candidate in candidates:
        for base in (clone_dir / "src" / candidate, clone_dir / candidate):
            if (base / "__init__.py").is_file():
                return candidate
    return None


def list_repo_top_level_symbols(clone_dir: Path, pkg_name: str) -> set[str]:
    """Return the set of top-level class/function names defined anywhere in
    the repo's Python package. Used by the symbol-collision guard so that
    the LLM's `task_module.py` doesn't reuse a name that would let an agent
    solve the task by re-exporting the repo's own implementation.

    Cheap: one AST walk per .py file under the package directory.
    Errors are swallowed — corrupt files are treated as contributing no names.
    """
    for base in (clone_dir / "src" / pkg_name, clone_dir / pkg_name):
        if base.is_dir():
            root = base
            break
    else:
        return set()
    names: set[str] = set()
    for py in root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(node.name)
    return names


# ---------------------------------------------------------------------------
# Seed sampling
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Seed:
    """One sampled snippet from the target repo."""

    relative_path: str  # POSIX-style, e.g. "src/foo/bar.py"
    start_line: int  # 1-indexed
    end_line: int  # 1-indexed, inclusive
    text: str  # the snippet itself (no line numbers)


def is_excluded(relative_path: str, exclude_globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(relative_path, pat) for pat in exclude_globs)


def list_source_files(clone_dir: Path, *, file_glob: str, exclude_glob: list[str]) -> list[Path]:
    """Walk the repo tree matching `file_glob` minus `exclude_glob`."""
    out: list[Path] = []
    for p in clone_dir.glob(file_glob):
        if not p.is_file():
            continue
        rel = str(p.relative_to(clone_dir))
        if is_excluded(rel, exclude_glob):
            continue
        out.append(p)
    return out


def sample_seed(
    files: list[Path],
    clone_dir: Path,
    *,
    rng: random.Random,
    min_loc: int,
    max_loc: int,
    max_attempts: int = 20,
) -> Seed | None:
    """Pick a random file, pick a random window of `[min_loc..max_loc]` lines.

    Skips files that are too short. Skips windows that are dominated by
    blank lines, imports, docstrings, or comments (those snippets give
    the LLM nothing to work with). Returns None after `max_attempts`.
    """
    if not files:
        return None
    for _ in range(max_attempts):
        f = rng.choice(files)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if len(lines) < min_loc:
            continue
        window = rng.randint(min_loc, min(max_loc, len(lines)))
        start = rng.randint(0, max(0, len(lines) - window))
        end = start + window
        chunk_lines = lines[start:end]
        chunk = "\n".join(chunk_lines)
        if _looks_substantive(chunk_lines):
            return Seed(
                relative_path=str(f.relative_to(clone_dir)),
                start_line=start + 1,
                end_line=end,
                text=chunk,
            )
    return None


_COMMENT_OR_IMPORT_RE = re.compile(r"^\s*(?:#|from\s|import\s)")


def _looks_substantive(chunk_lines: list[str]) -> bool:
    """Reject snippets that are 80%+ comments / imports / blanks."""
    if not chunk_lines:
        return False
    boring = 0
    for ln in chunk_lines:
        s = ln.strip()
        if not s or _COMMENT_OR_IMPORT_RE.match(s):
            boring += 1
    return (boring / len(chunk_lines)) < 0.8


# ---------------------------------------------------------------------------
# Decontamination — substring match against well-known eval benchmarks
# ---------------------------------------------------------------------------


# A minimal seed set. The real Magicoder corpus is huge (~10K+ substrings);
# we cover the most common contamination vectors. Extend per-need.
DEFAULT_BENCHMARK_PHRASES: tuple[str, ...] = (
    # HumanEval-style canonical phrasings (both definition and bare-name forms)
    "from typing import list",
    "def has_close_elements",
    "has_close_elements",
    "def separate_paren_groups",
    "separate_paren_groups",
    "def truncate_number",
    "truncate_number",
    # MBPP common phrasings
    "write a python function to",
    "write a function to find the",
    # APPS competitive phrasings
    "the first line of input contains an integer",
    "given a non-empty array of integers",
    # GSM8K (math word problems)
    "natalia sold clips to",
    "if a car travels",
    # DS-1000
    "import pandas as pd",
    "import numpy as np",
)


def has_benchmark_overlap(text: str, phrases: tuple[str, ...] = DEFAULT_BENCHMARK_PHRASES) -> bool:
    """True if any known benchmark phrase appears as a substring (case-insensitive)."""
    lower = text.lower()
    return any(p.lower() in lower for p in phrases)


# ---------------------------------------------------------------------------
# Prompting + parsing
# ---------------------------------------------------------------------------


PROMPT_SYSTEM = """You are a senior Python engineer designing a programming exercise that is genuinely anchored in a specific open-source library. You will be given a code snippet from the library `{pkg_name}` and asked to produce a task that requires the solver to USE `{pkg_name}`'s public API to solve it.

The goal is a task that CANNOT be solved without importing from `{pkg_name}` — a solver working in an isolated file without the library available would be stuck.

Produce three sections — exactly in this order, exactly with these section headers:

[Problem Description]
A clear problem statement that names `{pkg_name}` explicitly. Describe what to implement, what `{pkg_name}` primitive(s) to build on (subclass, extend, wrap, or compose), and the expected behavior. Include 1-2 short input/output examples. Under 200 words.

[Test]
A pytest test file. It MUST `from task_module import ...` (literal module name). Write 3-6 assertions covering normal cases AND edge cases (including at least one `pytest.raises` on an error condition). Use plain `def test_...(): assert ...` — no fixtures. The test may also import from `{pkg_name}` to construct inputs or verify behavior against the library's own types.

[Solution]
The Python source for `task_module.py`. It MUST contain at least one `from {pkg_name} import ...` (or `import {pkg_name}`) that is actually used by the code — subclass a `{pkg_name}` class, call a `{pkg_name}` function, wrap a `{pkg_name}` type, etc. Provide a complete, runnable Python file. Beyond `{pkg_name}` and the Python stdlib, do NOT import any other third-party libraries.

Do NOT name any top-level class or function the same as an existing `{pkg_name}` public symbol — the task-module symbol must be new (e.g. prefix or suffix it so a `grep` of the library source would not find it verbatim).

Output only those three sections in that order. No preamble, no closing notes."""


PROMPT_USER_TEMPLATE = """Target library: `{pkg_name}`.

Inspiration snippet (from `{path}`, lines {start}-{end}):

```python
{snippet}
```

Design a task that requires the solver to import and use `{pkg_name}`'s public API. The solution's `task_module.py` MUST contain a used `from {pkg_name} import ...` (or `import {pkg_name}`). Do not reuse any existing `{pkg_name}` symbol name for your task-module's top-level classes/functions."""


@dataclass(slots=True)
class ParsedTask:
    problem: str
    test_code: str
    solution_code: str


def parse_task_response(text: str) -> ParsedTask | None:
    """Extract the three sections from the LLM's response.

    Tolerant: section markers are matched case-insensitively. Returns
    None if any section is missing or empty.
    """
    problem = _extract_section(text, "Problem Description")
    test = _extract_section(text, "Test")
    solution = _extract_section(text, "Solution")
    if not (problem and test and solution):
        return None
    return ParsedTask(
        problem=problem.strip(),
        test_code=_strip_code_fence(test),
        solution_code=_strip_code_fence(solution),
    )


_SECTION_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _section_re(name: str) -> re.Pattern[str]:
    if name not in _SECTION_RE_CACHE:
        # Match `[Section Name]` markers, with optional leading/trailing whitespace
        pattern = rf"(?im)^\s*\[\s*{re.escape(name)}\s*\]\s*$"
        _SECTION_RE_CACHE[name] = re.compile(pattern)
    return _SECTION_RE_CACHE[name]


def _extract_section(text: str, name: str) -> str:
    """Return the text between `[<name>]` and the next `[...]` marker (or EOF)."""
    pattern = _section_re(name)
    m = pattern.search(text)
    if not m:
        return ""
    start = m.end()
    # Find the next section marker after this one
    next_marker = re.search(r"(?im)^\s*\[\s*[A-Za-z][A-Za-z ]+\s*\]\s*$", text[start:])
    end = start + next_marker.start() if next_marker else len(text)
    return text[start:end].strip()


_CODE_FENCE_RE = re.compile(r"^```(?:python|py)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Strip a single surrounding ``` code fence if present."""
    text = text.strip()
    m = _CODE_FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text


# ---------------------------------------------------------------------------
# Test syntactic validation
# ---------------------------------------------------------------------------


def references_task_module(test_code: str) -> bool:
    """True iff the test code imports from `task_module` (our convention).

    Without this check, the LLM could write a self-sufficient test that
    passes whether or not we apply the oracle — making the task trivial.

    Named without a `test_` prefix so pytest doesn't try to collect this
    helper as a test function.
    """
    pattern = re.compile(
        r"^\s*(?:from\s+task_module\s+import|import\s+task_module)\b", re.MULTILINE
    )
    return bool(pattern.search(test_code))


# ---------------------------------------------------------------------------
# Quality gates — the four post-LLM checks
# ---------------------------------------------------------------------------


def check_repo_anchoring(solution_code: str, pkg_name: str) -> tuple[bool, str]:
    """The oracle `task_module.py` must import the target repo and USE it.

    Passes iff:
      1. AST parses.
      2. At least one `Import`/`ImportFrom` node names `pkg_name` (or a
         submodule `pkg_name.foo`).
      3. Any of the imported names appear in the file body (not just the
         import line). "Used" here is a coarse substring/name check on the
         rest of the source — cheap and catches the common failure mode of
         a bare `import X` that never references X.

    Returns (accepted, reason). Reason is empty on accept.
    """
    try:
        tree = ast.parse(solution_code)
    except SyntaxError as exc:
        return False, f"solution_syntax_error:{exc.msg}"
    imported: list[str] = []  # local names introduced by the imports
    has_repo_import = False
    import_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and (node.module == pkg_name or node.module.startswith(f"{pkg_name}.")):
                has_repo_import = True
                import_lines.add(node.lineno)
                for alias in node.names:
                    imported.append(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == pkg_name or alias.name.startswith(f"{pkg_name}."):
                    has_repo_import = True
                    import_lines.add(node.lineno)
                    imported.append(alias.asname or alias.name.split(".", 1)[0])
    if not has_repo_import:
        return False, "no_repo_import"
    # Check "used" — any imported name appears outside its import line
    lines = solution_code.splitlines()
    body_text = "\n".join(ln for i, ln in enumerate(lines, 1) if i not in import_lines)
    for name in imported:
        # `*` from a star-import — impossible to verify usage; accept as used
        if name == "*":
            return True, ""
        if re.search(rf"\b{re.escape(name)}\b", body_text):
            return True, ""
    return False, "repo_import_unused"


def check_symbol_collision(solution_code: str, repo_symbols: set[str]) -> tuple[bool, str]:
    """Reject if any top-level class/function name in the oracle collides
    with a real symbol in the target repo — that lets an agent `grep`
    the on-disk source and re-export the real implementation to pass the
    binary test.
    """
    try:
        tree = ast.parse(solution_code)
    except SyntaxError:
        # already caught by anchoring check; don't double-report
        return True, ""
    for node in tree.body:
        if (
            isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in repo_symbols
        ):
            return False, f"symbol_collides_with_repo:{node.name}"
    return True, ""


_ASSERT_TRIVIAL_RE = re.compile(
    r"^\s*assert\s+(True|1|\".+\"|'.+')\s*(?:,.*)?\s*$",
)


def check_test_strength(test_code: str, instruction: str) -> tuple[bool, str]:
    """Reject test files that are too weak to be worth grading.

    Rules:
      1. Parses cleanly.
      2. At least 3 top-level `test_*` `def`s OR one `test_*` def with
         ≥3 non-trivial `assert` statements. (Multiple tests preferred.)
      3. Total non-trivial `assert` statements across the file ≥ 3.
      4. If the instruction mentions an error condition (raise/error/
         invalid/must not/reject), the test file must contain at least
         one `pytest.raises(...)` block.
      5. No `assert True` / `assert 1` / `assert "literal"`.
    """
    try:
        tree = ast.parse(test_code)
    except SyntaxError as exc:
        return False, f"test_syntax_error:{exc.msg}"
    test_fns: list[ast.FunctionDef] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            test_fns.append(node)
    if not test_fns:
        return False, "no_test_functions"

    total_asserts = 0
    trivial_asserts = 0
    for fn in test_fns:
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Assert):
                total_asserts += 1
                # `assert True`, `assert False`, `assert <literal>`
                test = sub.test
                if isinstance(test, ast.Constant):
                    if test.value in (True, False) or isinstance(test.value, (int, str)):
                        trivial_asserts += 1
                # `assert x == x`
                elif (
                    isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and len(test.comparators) == 1
                    and isinstance(test.comparators[0], ast.Name)
                    and test.left.id == test.comparators[0].id
                ):
                    trivial_asserts += 1

    non_trivial = total_asserts - trivial_asserts
    if trivial_asserts > 0:
        return False, "trivial_assert_present"
    if non_trivial < 3:
        return False, "too_few_asserts"

    # Error-branch coverage
    err_re = re.compile(r"\b(?:raise|error|invalid|must not|reject|forbidden)\b", re.IGNORECASE)
    if (
        err_re.search(instruction)
        and "pytest.raises" not in test_code
        and "raises(" not in test_code
    ):
        return False, "missing_pytest_raises"

    return True, ""


def task_fingerprints(problem: str, solution_code: str = "") -> set[str]:
    """Return the set of dedup signals for a candidate task.

    Two independent signals — a match on EITHER counts as a duplicate:
      1. Problem-statement head (first 80 normalized chars) — catches
         reworded-but-same-task drift.
      2. Sorted top-level public symbol names in the oracle — catches the
         opposite failure mode where the LLM keeps the same class (e.g.
         `RangedFloatType`) but reframes the problem statement.

    Private/dunder names are ignored so that internal helper renames
    don't split an otherwise-duplicate task into two fingerprints.
    """
    fps: set[str] = set()
    norm = re.sub(r"\s+", " ", problem.strip().lower())
    fps.add("p:" + hashlib.sha256(norm[:80].encode("utf-8")).hexdigest()[:16])
    if solution_code:
        try:
            tree = ast.parse(solution_code)
        except SyntaxError:
            return fps
        symbols = sorted(
            n.name
            for n in tree.body
            if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and not n.name.startswith("_")
        )
        if symbols:
            fps.add("s:" + hashlib.sha256(",".join(symbols).encode("utf-8")).hexdigest()[:16])
    return fps


def task_fingerprint(problem: str, solution_code: str = "") -> str:
    """Legacy single-fingerprint (problem-only) — kept for compat.

    New callers should use `task_fingerprints` which returns a set of
    independent signals covering both problem-rewording and shared-class
    duplication modes.
    """
    norm = re.sub(r"\s+", " ", problem.strip().lower())
    return hashlib.sha256(norm[:80].encode("utf-8")).hexdigest()[:16]
