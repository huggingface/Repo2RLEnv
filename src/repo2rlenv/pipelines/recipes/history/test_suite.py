"""Stage selected historical tests without breaking their package fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

from repo2rlenv.pipelines.recipes.history.selection import within


def stage_tests(head: Path, old: Path, selected: list[str], roots: list[str]) -> list[str]:
    """Keep helper files private while executing only the selected test modules.

    Preserve names and relative imports beneath an outer directory that is not
    a Python package. Pytest can then import the original test package instead
    of interpreting its modules as renamed, unrelated top-level files.
    """
    if not selected or any(not within(path, roots) for path in selected):
        raise ValueError("Selected tests must belong to the configured test roots")
    suite = old / "r2e_tests"
    if suite.exists():
        raise ValueError("Input repository already owns reserved r2e_tests directory")
    suite.mkdir()
    for root in sorted(set(roots), key=lambda value: (len(Path(value).parts), value)):
        source, target = head / root, suite / root
        if target.exists():
            continue  # A containing configured root already copied this path.
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            shutil.copyfile(source, target)
        else:
            raise ValueError(f"Historical test root is missing: {root}")
    if any(not (suite / path).is_file() for path in selected):
        raise ValueError("Selected historical test module is missing")
    return ["r2e_tests/" + path for path in selected]
