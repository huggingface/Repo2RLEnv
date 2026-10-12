"""Convenient entry point for Repo2RLEnv's Tasksmith pipeline."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Run ``repo2rlenv tasksmith`` with the supplied arguments and exit status."""
    from repo2rlenv.cli import main as repo2rlenv_main

    args = list(sys.argv[1:] if argv is None else argv)
    return repo2rlenv_main(["tasksmith", *args])
