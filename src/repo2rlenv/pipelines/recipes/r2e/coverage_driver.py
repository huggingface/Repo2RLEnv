"""Measure target branch coverage from generated tests during a full pytest run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import coverage
import pytest


class GeneratedTestCoverage:
    def __init__(self, source: str, generated_test_path: str = "tests/test_r2e_generated.py"):
        self.generated_test_path = generated_test_path
        self.coverage = coverage.Coverage(
            branch=True, data_file=None, config_file=False, include=["/workspace/" + source]
        )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        measured = item.nodeid.split("::", 1)[0] == self.generated_test_path
        if measured:
            self.coverage.start()
        try:
            yield
        finally:
            if measured:
                self.coverage.stop()


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("source")
    parser.add_argument("--generated-test-path", default="tests/test_r2e_generated.py")
    args, pytest_args = parser.parse_known_args(sys.argv[1:])
    plugin = GeneratedTestCoverage(args.source, args.generated_test_path)
    Path("/tmp/observations.json").write_text("[]")
    code = pytest.main(pytest_args, plugins=[plugin])
    try:
        plugin.coverage.json_report(outfile="/tmp/coverage.json")
    except coverage.exceptions.NoDataError:
        Path("/tmp/coverage.json").write_text(json.dumps({"files": {}}))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
