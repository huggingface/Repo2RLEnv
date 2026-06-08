"""Shared verifier-script + diff helpers for synthesis pipelines.

These were originally defined in `mutation_bugs.py`; they outlived that
pipeline and are used by `code_instruct` and `equivalence_tests` to build
their `tests/test.sh` (binary pass/fail reward) and their gold patches.
"""

from __future__ import annotations

import difflib

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
