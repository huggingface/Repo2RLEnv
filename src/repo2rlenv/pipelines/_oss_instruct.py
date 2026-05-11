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

import fnmatch
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


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


PROMPT_SYSTEM = """You are a senior Python engineer. You will be given a code snippet from an open-source repository. Your job is to design a new, self-contained programming exercise that is INSPIRED by the snippet but does NOT require any of the repo's APIs.

Produce three sections — exactly in this order, exactly with these section headers:

[Problem Description]
A clear, self-contained problem statement. Describe the function to implement and its expected behavior. Include 1-2 examples. Avoid any specific library or framework references. Treat the reader as someone who has not seen the snippet. Keep it under 200 words.

[Test]
A pytest test file. It MUST import from the module `task_module` (literal name, do not change it). Write 2-4 assertions covering normal cases AND edge cases. Use plain `def test_...(): assert ...` — no fixtures.

[Solution]
The Python source for `task_module.py` — the implementation the test will exercise. Provide a complete, runnable Python file. Do NOT import any third-party libraries; only Python stdlib is allowed.

Output only those three sections in that order. No preamble, no closing notes."""


PROMPT_USER_TEMPLATE = """Inspiration snippet (from `{path}`, lines {start}-{end}):

```python
{snippet}
```

Design a NEW, self-contained programming exercise inspired by the snippet."""


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
