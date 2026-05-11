"""Per-language test output parsers.

Each parser turns the raw stdout/stderr of a test runner into a
`dict[test_name -> status]`, where status ∈ {PASSED, FAILED, SKIPPED, ERROR}.

The validation harness for `pr_runtime` uses these to compute FAIL_TO_PASS
and PASS_TO_PASS sets per SWE-bench's grading semantics. Each parser is
intentionally simple — heuristic line matching, not full structured-output
parsing — because the runners' output formats are stable enough across
versions.

Acknowledgment: pytest parser shape adapted from SWE-bench's
harness/log_parsers/python.py. Independent implementation, Apache-2.0.
"""

from __future__ import annotations

from repo2rlenv.log_parsers.pytest_parser import parse_pytest

__all__ = ["parse_pytest"]
