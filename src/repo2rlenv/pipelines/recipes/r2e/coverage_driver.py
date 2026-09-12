"""Measure target branch coverage from generated tests during a full pytest run."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import coverage
import pytest


class GeneratedTestCoverage:
    def __init__(self, source: str):
        self.coverage = coverage.Coverage(
            branch=True, data_file=None, config_file=False, include=["/workspace/" + source]
        )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        measured = item.nodeid.startswith("tests/test_r2e_generated.py::")
        if measured:
            self.coverage.start()
        try:
            yield
        finally:
            if measured:
                self.coverage.stop()


def main() -> None:
    plugin = GeneratedTestCoverage(sys.argv[1])
    Path("/tmp/observations.json").write_text("[]")
    code = pytest.main(sys.argv[2:], plugins=[plugin])
    try:
        plugin.coverage.json_report(outfile="/tmp/coverage.json")
    except coverage.exceptions.NoDataError:
        Path("/tmp/coverage.json").write_text(json.dumps({"files": {}}))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
