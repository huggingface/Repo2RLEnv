"""`python -m unittest` / Django test-runner output parser.

unittest's verbose runner prints one line per test, with the status after an
ellipsis:

    test_add (tests.test_math.MathTests.test_add) ... ok
    test_broken (tests.test_math.MathTests.test_broken) ... FAIL
    test_error (tests.test_math.MathTests.test_error) ... ERROR
    test_skipped (tests.test_math.MathTests.test_skipped) ... skipped 'later'

Two shapes need care:

  * A test with a docstring prints the name, then the docstring and the
    status on the NEXT line, because `getDescription` joins them with a
    newline. The status therefore belongs to the name printed just above.
  * A subtest failure is reported on its own indented line carrying the
    parameters (`... (i=2) ... FAIL`), while the parent test's line ends
    after the ellipsis with no status at all. Subtests are recorded under
    the parent's identity, and the worst status wins, because the repaired
    run prints only `parent ... ok`: keeping the parameters would leave
    FAIL_TO_PASS with a name that never appears again. Named subtests add a
    message before their parameters (`[edge case] (value=(1, 2))`), so the
    whole suffix is matched loosely and discarded.

Test names are canonicalized to the dotted id you could re-run, so the key
is stable across Python versions: 3.11+ prints the full path inside the
parentheses, older versions print only the class path with the method name
outside.

`expected failure` counts as PASSED and `unexpected success` as FAILED,
matching unittest's own verdict (`wasSuccessful()`), so the F2P/P2P sets
agree with the suite's exit code.

Django's runner (`manage.py test -v 2`) delegates to unittest's
TextTestRunner, so this parses its output too.

Released under Apache-2.0.
"""

from __future__ import annotations

import re

from repo2rlenv.log_parsers.pytest_parser import TestStatus

# `test_add (pkg.mod.Class.test_add)` plus any subtest description, e.g.
# ` [edge case] (i=2)` or ` (value=(1, 2))`, which is matched but not kept.
_NAME_RE = re.compile(
    r"^(?P<method>[^\s()]+) \((?P<dotted>[\w.]+)\)(?: .+)?$",
)

_STATUS_WORDS: dict[str, TestStatus] = {
    "ok": "PASSED",
    "FAIL": "FAILED",
    "ERROR": "ERROR",
    "expected failure": "PASSED",
    "unexpected success": "FAILED",
}

# Failure blocks below the run repeat each name: `FAIL: test_x (pkg.Class.test_x)`.
_BLOCK_RE = re.compile(r"^(?P<status>FAIL|ERROR):\s+(?P<name>.+?)\s*$")


# Worst status wins when a test reports more than once, i.e. a parent whose
# subtests each report separately.
_RANK: dict[TestStatus, int] = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2, "ERROR": 3}


def _canonical(method: str, dotted: str) -> str:
    """Return the dotted id, e.g. `tests.test_math.MathTests.test_add`.

    3.11+ prints the full path inside the parentheses; older versions print
    the class path there and the method name outside.
    """
    return dotted if dotted.endswith(f".{method}") else f"{dotted}.{method}"


def _record(out: dict[str, TestStatus], name: str, status: TestStatus) -> None:
    if name in out and _RANK[out[name]] >= _RANK[status]:
        return
    out[name] = status


def _status_for(tail: str) -> TestStatus | None:
    """Map the text after the ellipsis to a status, or None when unknown."""
    text = tail.strip()
    if not text:
        # A parent test whose subtests report separately.
        return None
    if text.startswith("skipped"):
        return "SKIPPED"
    return _STATUS_WORDS.get(text)


def parse_unittest(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from unittest/Django output.

    Lines that are neither a result line nor a failure-block header are
    ignored, so tracebacks and the `Ran N tests` footer contribute nothing.
    """
    out: dict[str, TestStatus] = {}
    if not log:
        return out

    # A name printed without a status is only claimed by the very next line.
    pending: str | None = None
    for raw in log.split("\n"):
        line = raw.rstrip()
        claimed, pending = pending, None
        if not line.strip():
            continue

        # A named subtest message may itself contain ` ... `, so split at the
        # final separator before the result word.
        head, sep, tail = line.strip().rpartition(" ... ")
        if sep:
            m = _NAME_RE.match(head)
            if m:
                status = _status_for(tail)
                if status is None:
                    # Parent of subtests: its own results follow, indented.
                    continue
                _record(out, _canonical(m["method"], m["dotted"]), status)
            elif claimed is not None:
                # `<docstring> ... ok` for the name on the previous line.
                status = _status_for(tail)
                if status is not None:
                    _record(out, claimed, status)
            continue

        m = _NAME_RE.match(line.strip())
        if m:
            pending = _canonical(m["method"], m["dotted"])
            continue

        m = _BLOCK_RE.match(line)
        if m:
            name = _NAME_RE.match(m["name"])
            if name:
                key = _canonical(name["method"], name["dotted"])
                _record(out, key, _STATUS_WORDS[m["status"]])

    return out
