"""Per-language test output parsers.

Each parser turns the raw stdout/stderr of a test runner into a
`dict[test_name -> status]`, where status ∈ {PASSED, FAILED, SKIPPED, ERROR}.

The validation harness for `pr_runtime` uses these to compute FAIL_TO_PASS
and PASS_TO_PASS sets per SWE-bench's grading semantics. Each parser is
intentionally simple — heuristic line matching, not full structured-output
parsing — because the runners' output formats are stable enough across
versions.

Public API:
  * `parse_pytest`    — pytest
  * `parse_go_test`   — `go test -v`
  * `parse_cargo_test`— `cargo test`
  * `parse_jest`      — Jest / Mocha / Vitest

  * `parse_logs(language, test_cmds, log)` — dispatch by runner. Inspects
    the test_cmds string for runner keywords first; falls back to the
    LanguageHint when ambiguous. Returns the parser's status map.

Acknowledgment: pytest parser shape adapted from SWE-bench's
harness/log_parsers/python.py. Other parsers are original implementations
written against each runner's canonical text output. Apache-2.0.
"""

from __future__ import annotations

import re

from repo2rlenv.log_parsers.cargo_parser import parse_cargo_test
from repo2rlenv.log_parsers.go_parser import parse_go_test
from repo2rlenv.log_parsers.jest_parser import parse_jest
from repo2rlenv.log_parsers.pytest_parser import TestStatus, parse_pytest

__all__ = [
    "TestStatus",
    "parse_cargo_test",
    "parse_go_test",
    "parse_jest",
    "parse_logs",
    "parse_pytest",
]


def _detect_runner(test_cmds: list[str]) -> str:
    """Inspect the bootstrap-recorded test commands and name the runner.

    Returns one of: "pytest", "go", "cargo", "jest", "unknown".

    Why inspect test_cmds rather than the LanguageHint alone? A single
    language often has multiple runners — Python has pytest + unittest +
    Django's test runner; JS has jest + mocha + vitest. The test_cmds
    string the bootstrap agent recorded is the source of truth for what
    will actually run.
    """
    joined = " ".join(test_cmds).lower()
    if "pytest" in joined:
        return "pytest"
    if re.search(r"\bgo\s+test\b", joined):
        return "go"
    if re.search(r"\bcargo\s+test\b", joined):
        return "cargo"
    # Jest is usually invoked via `npm test`, `npx jest`, `yarn test`, or
    # plain `jest`. Mocha + vitest produce jest-compatible output and we
    # parse them with the same parser.
    if "jest" in joined or "mocha" in joined or "vitest" in joined:
        return "jest"
    if "npm test" in joined or "yarn test" in joined or "pnpm test" in joined:
        return "jest"
    return "unknown"


def parse_logs(
    test_cmds: list[str],
    log: str,
    *,
    language: str | None = None,
) -> dict[str, TestStatus]:
    """Dispatch to the right per-runner parser based on test_cmds.

    Args:
        test_cmds: The bootstrap-recorded commands that ran (e.g.
            `["pytest -v"]` or `["go test ./..."]`). Used to detect the
            test runner.
        log: The captured stdout/stderr from the test invocation.
        language: Optional LanguageHint value (e.g. "python", "go") used
            only as a last-resort fallback when test_cmds doesn't name a
            known runner. Almost always inferred from test_cmds anyway.

    Returns the {test_name -> status} map. Returns an empty dict if no
    runner can be identified — caller should treat that as "test suite
    didn't produce parseable output, treat as env issue".
    """
    runner = _detect_runner(test_cmds)
    # Language fallback when test_cmds is unhelpful (e.g. a wrapper script)
    if runner == "unknown" and language:
        lang_default = {
            "python": "pytest",
            "go": "go",
            "rust": "cargo",
            "node": "jest",
        }.get(language.lower(), "unknown")
        runner = lang_default

    if runner == "pytest":
        return parse_pytest(log)
    if runner == "go":
        return parse_go_test(log)
    if runner == "cargo":
        return parse_cargo_test(log)
    if runner == "jest":
        return parse_jest(log)
    return {}
