"""Every text-mode subprocess call must decode its output as UTF-8.

`text=True` alone decodes with the locale encoding: UTF-8 on Linux and macOS,
but the ANSI code page (e.g. cp1252) on Windows. There, gh / git / docker output
containing non-ASCII came back empty (the UnicodeDecodeError dies in
subprocess's reader thread, leaving stdout=None) or garbled: a PR diff's `├──`
became `â”œâ”€â”€` in solution/patch.diff. CI runs on Linux, where the bug can't
show, so this static check keeps new calls explicit.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import repo2rlenv

SRC = Path(repo2rlenv.__file__).parent


def _text_mode_calls_without_encoding() -> Iterator[str]:
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                continue
            keywords = {k.arg for k in node.keywords if k.arg}
            if keywords & {"text", "universal_newlines"} and "encoding" not in keywords:
                yield f"{path.relative_to(SRC).as_posix()}:{node.lineno}"


def test_text_mode_subprocess_calls_pass_an_encoding():
    offenders = list(_text_mode_calls_without_encoding())
    assert offenders == [], f'add encoding="utf-8" to: {offenders}'
