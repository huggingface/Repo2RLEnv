"""In-container verifier for pr_runtime tasks (graded F2P/P2P reward).

This module is the **standalone verifier** that runs inside the task's
Docker container, NOT a helper used at generation time. It is read as
source at generation time, base64-encoded, and embedded into
``tests/test.sh``. At run time the container decodes it back to a file
and invokes it after the test suite has run.

Why a graded reward instead of binary pass/fail
------------------------------------------------
SWE-bench resolution is binary: a patch "resolves" the issue iff ALL
FAIL_TO_PASS tests pass AND ALL PASS_TO_PASS tests still pass. That's
the right signal for an eval leaderboard, but a terrible gradient for
RL training — an agent that fixes 4 of 5 failing tests scores the same
0.0 as one that fixes nothing.

So this verifier emits BOTH:
  * /logs/verifier/reward.txt  — the GRADED scalar (training signal,
        which Harbor reads):  reward = f2p_rate * p2p_factor
  * /logs/verifier/reward-details.json — carries the strict SWE-bench
        ``resolved`` bool (eval signal) PLUS the full breakdown.

Refs: SWE-bench (F2P/P2P semantics), SWE-RL / SWE-Gym (dense reward for
RL), UTBoost (weak-test coverage lets wrong patches pass — hence we
record p2p_count so consumers can judge regression-guard strength).

Scoring
-------
Given the baked FAIL_TO_PASS / PASS_TO_PASS test-name lists (computed by
the generation-time two-stage validation) and the agent-run test log:

  f2p_rate   = (# F2P tests now PASSED) / (# F2P tests)
  p2p_rate   = (# P2P tests still PASSED) / (# P2P tests)   [1.0 if no P2P]
  p2p_factor = p2p_rate          # regressions scale the reward down
  reward     = f2p_rate * p2p_factor
  resolved   = (all F2P pass) AND (all P2P pass)            # strict SWE-bench

Oracle invariant: the gold patch flips every F2P and keeps every P2P,
so f2p_rate=1.0, p2p_rate=1.0 -> reward=1.0 and resolved=True. This is
what the T3 oracle gate (reward == 1.0) relies on.

Graceful degradation: if the log can't be parsed into per-test
statuses (unrecognized runner output) and no F2P oracle is declared,
fall back to the exit-code reward (1.0 if the suite exited 0, else 0.0)
and stamp ``parse_status="fallback_exitcode"`` — never crash. With an
F2P oracle declared, the fallback reward is 0.0: generation already
parsed this runner's output to build the F2P list, so an unparseable
log provides no evidence that the declared tests passed. It can indicate
an environment or parser failure as well as tampering.

A test reported more than once keeps its worst status (ERROR > FAILED >
PASSED > SKIPPED), so a fake ``PASSED`` line printed by code under test
can't overwrite pytest's real ``FAILED``.

Pure stdlib — uses only ``argparse``, ``json``, ``os``, ``re``, ``stat``, ``sys``.
JUnit reports (code_instruct, equivalence_tests) go through the repository
recipe's ``test_results.parse_junit``, shipped next to this file.
The 4 per-runner parsers are condensed ports of
``repo2rlenv.log_parsers.*`` kept in lockstep via the unit tests under
``tests/test_pr_runtime_verifier.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys

# Canonical statuses. (Plain strings — no typing.Literal so the baked
# module stays import-light when decoded standalone in the container.)
PASSED = "PASSED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
ERROR = "ERROR"

# ---------------------------------------------------------------------------
# Per-runner log parsers (condensed ports of repo2rlenv.log_parsers.*)
# ---------------------------------------------------------------------------

_PYTEST_STATUSES = (PASSED, FAILED, SKIPPED, ERROR)
# Keep these patterns in sync with log_parsers/pytest_parser.py. This module
# also runs standalone inside task containers, without a repo2rlenv install.
_PYTEST_PROGRESS = (
    r"\[\s*\d+%\]|\[\s*\d+\s*/\s*\d+\s*\]"
    r"|\d+(?:\.\d+)?(?:us|ms|s)|\d+m \d+s|\d+h \d+m"
)
_PYTEST_PROGRESS_RE = re.compile(_PYTEST_PROGRESS)
_WHITESPACE_RE = re.compile(r"\s")


def _verbose_match(line: str) -> tuple[str, str] | None:
    """(name, status) of a verbose result line, exactly as
    `^(.+?(?:\\[.*?\\])?)\\s+(PASSED|FAILED|SKIPPED|ERROR)(?:\\s+\\(.*\\))?(?:\\s+(?:PROGRESS))?$`
    reads it, in linear time (that regex backtracked quadratically).

    The regex takes the shortest name, trying at each length first a bracket
    group closed by the first workable "]", then the bare name. A name end E
    works when the rest of the line, from E, is whitespace, a status word and
    an optional "(reason)" and/or progress field up to the end of the line.
    """
    size = len(line)
    if size < 2:
        return None
    space = [_WHITESPACE_RE.match(char) is not None for char in line]
    # next_solid[i]: the first non-whitespace position at or after i.
    next_solid = [size] * (size + 1)
    for index in range(size - 1, -1, -1):
        next_solid[index] = next_solid[index + 1] if space[index] else index
    # Positions where a progress field starts after whitespace and runs to the end.
    progress = {
        start
        for start in range(1, size)
        if space[start - 1] and _PYTEST_PROGRESS_RE.fullmatch(line, start) is not None
    }
    # The last ")" that can close a "(reason)": the line's last character, or
    # the ")" right before the whitespace that precedes a progress field.
    last_close = size - 1 if line[-1] == ")" else -1
    for start in progress:
        run = start - 1
        while run > 0 and space[run - 1]:
            run -= 1
        if run > 0 and line[run - 1] == ")":
            last_close = max(last_close, run - 1)

    def tail_ok(position: int) -> bool:
        if position == size:
            return True
        solid = next_solid[position]
        if solid in (position, size):
            return False
        return solid in progress or (line[solid] == "(" and last_close > solid)

    def rest(end: int) -> str | None:
        if end >= size or not space[end]:
            return None
        solid = next_solid[end]
        for status in _PYTEST_STATUSES:
            if line.startswith(status, solid) and tail_ok(solid + len(status)):
                return status
        return None

    # good_close[i]: the first "]" at or after i whose next position works as a name end.
    good_close = [-1] * (size + 1)
    for index in range(size - 1, -1, -1):
        good_close[index] = (
            index if line[index] == "]" and rest(index + 1) else good_close[index + 1]
        )
    for length in range(1, size):
        if line[length] == "[" and good_close[length + 1] >= 0:
            end = good_close[length + 1] + 1
            return line[:end], rest(end)  # type: ignore[return-value]
        status = rest(length)
        if status is not None:
            return line[:length], status
    return None


# pytest-xdist worker label, e.g. `[gw1] [ 20%] PASSED tests/foo.py::test_x`.
_PYTEST_XDIST_PREFIX_RE = re.compile(rf"^\[gw\d+\]\s+(?:(?:{_PYTEST_PROGRESS})\s+)?")
# Keep the broad pytest substring and bounded py.test alias in sync with the
# package-side detector in log_parsers/__init__.py.
_PYTEST_RUNNER_RE = re.compile(r"pytest|\bpy[.]test\b")
# A test reported more than once keeps its worst status. Code under test
# shares the log with pytest, so last-write-wins would let it print
# `PASSED <node id>` after pytest's real `FAILED` and flip the result.
_PYTEST_RANK = {SKIPPED: 0, PASSED: 1, FAILED: 2, ERROR: 3}


def _summary_name(work: str) -> str:
    """The node ID of a summary line, as `^(.+?(?:\\[.*?\\])?)(?: - .*)?$` reads
    it, in linear time (that regex backtracked quadratically on bracket-heavy
    lines): the shortest prefix that ends where the line does or where " - "
    starts, optionally extended by a bracket group to the first "]" that is
    itself followed by the end or " - "."""
    size = len(work)

    def cut(position: int) -> bool:
        return position == size or work.startswith(" - ", position)

    # closing[i]: the first "]" at or after i that is followed by a cut.
    closing = [-1] * (size + 1)
    for index in range(size - 1, -1, -1):
        closing[index] = index if work[index] == "]" and cut(index + 1) else closing[index + 1]
    for length in range(1, size + 1):
        if length < size and work[length] == "[" and closing[length] >= 0:
            return work[: closing[length] + 1]
        if cut(length):
            return work[:length]
    return work


def _record_worst(out: dict[str, str], name: str, status: str) -> None:
    if name in out and _PYTEST_RANK[out[name]] >= _PYTEST_RANK[status]:
        return
    out[name] = status


def parse_pytest(log: str) -> dict[str, str]:
    """{test_name -> status} from pytest output (verbose or summary)."""
    out: dict[str, str] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        line = raw.strip()
        if not line:
            continue
        line = _PYTEST_XDIST_PREFIX_RE.sub("", line, count=1)
        # Summary lines (STATUS first), preserving the full node ID.
        leading = None
        for st in _PYTEST_STATUSES:
            if line.startswith(st + " ") or line == st:
                leading = st
                break
        if leading is not None:
            work = line[len(leading) :].strip()
            # unittest's `FAILED (failures=1)` footer is a count, not a node ID.
            if work.startswith("(") and work.endswith(")"):
                continue
            if leading == SKIPPED and re.match(r"^\[\d+\](?:\s|$)", work):
                # Folded skips report a file location, not a parametrized ID.
                tokens = work.split(maxsplit=2)
                if len(tokens) < 2:
                    continue
                _record_worst(out, tokens[1], leading)
            elif work:
                _record_worst(out, _summary_name(work), leading)
            continue
        # Verbose progress (NAME first, STATUS after)
        # Filter non-test output before matching a verbose result line.
        head = line.split(None, 1)[0]
        if "::" not in head and not head.endswith(".py"):
            continue
        match = _verbose_match(line)
        if match:
            name, status = match
            if "::" in name or name.endswith(".py"):
                _record_worst(out, name, status)
    return out


# Keep these patterns in sync with log_parsers/unittest_parser.py.
_UT_NAME_RE = re.compile(r"^(?P<method>[^\s()]+) \((?P<dotted>[\w.]+)\)(?: .+)?$")
_UT_BLOCK_RE = re.compile(r"^(?P<status>FAIL|ERROR):\s+(?P<name>.+?)\s*$")
_UT_STATUS = {
    "ok": PASSED,
    "FAIL": FAILED,
    "ERROR": ERROR,
    "expected failure": PASSED,
    "unexpected success": FAILED,
}


# Subtests report under the parent's identity, worst status first, because a
# repaired run prints only `parent ... ok`.
_UT_RANK = {SKIPPED: 0, PASSED: 1, FAILED: 2, ERROR: 3}


def _ut_canonical(method: str, dotted: str) -> str:
    return dotted if dotted.endswith(f".{method}") else f"{dotted}.{method}"


def _ut_record(out: dict[str, str], name: str, status: str) -> None:
    if name in out and _UT_RANK[out[name]] >= _UT_RANK[status]:
        return
    out[name] = status


def _ut_status(tail: str) -> str | None:
    text = tail.strip()
    if not text:
        return None
    if text.startswith("skipped"):
        return SKIPPED
    return _UT_STATUS.get(text)


def parse_unittest(log: str) -> dict[str, str]:
    """{test_name -> status} from `python -m unittest -v` / Django output.

    A docstring pushes the status onto the line after the name, and subtest
    failures report on their own indented line while the parent prints none.
    """
    out: dict[str, str] = {}
    if not log:
        return out
    pending: str | None = None
    for raw in log.split("\n"):
        line = raw.rstrip()
        claimed, pending = pending, None
        if not line.strip():
            continue
        head, sep, tail = line.strip().rpartition(" ... ")
        if sep:
            m = _UT_NAME_RE.match(head)
            if m:
                status = _ut_status(tail)
                if status is not None:
                    _ut_record(out, _ut_canonical(m["method"], m["dotted"]), status)
            elif claimed is not None:
                status = _ut_status(tail)
                if status is not None:
                    _ut_record(out, claimed, status)
            continue
        m = _UT_NAME_RE.match(line.strip())
        if m:
            pending = _ut_canonical(m["method"], m["dotted"])
            continue
        m = _UT_BLOCK_RE.match(line)
        if m:
            name = _UT_NAME_RE.match(m["name"])
            if name:
                _ut_record(
                    out, _ut_canonical(name["method"], name["dotted"]), _UT_STATUS[m["status"]]
                )
    return out


_GO_TEST_RE = re.compile(r"^\s*---\s+(?P<status>PASS|FAIL|SKIP):\s+(?P<name>\S+)")
_GO_STATUS = {"PASS": PASSED, "FAIL": FAILED, "SKIP": SKIPPED}


def parse_go_test(log: str) -> dict[str, str]:
    """{test_name -> status} from `go test -v` output."""
    out: dict[str, str] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        m = _GO_TEST_RE.match(raw)
        if m:
            _record_worst(out, m.group("name"), _GO_STATUS[m.group("status")])
    return out


# Keep these patterns in sync with log_parsers/cargo_parser.py.
_CARGO_TEST_RE = re.compile(r"^test\s+(?P<name>\S.*?) \.\.\. (?P<status>ok|FAILED|ignored)\b")
_CARGO_MODE_RE = re.compile(r" - (?:should panic|compile fail|compile)$")
_CARGO_DOCTEST_RE = re.compile(r"^(?P<file>\S+) - (?:(?P<item>.+?) )?\(line \d+\)$")
_CARGO_STATUS = {"ok": PASSED, "FAILED": FAILED, "ignored": SKIPPED}
_CARGO_DOCTEST_RANK = {SKIPPED: 0, PASSED: 1, FAILED: 2}


def parse_cargo_test(log: str) -> dict[str, str]:
    """{test_name -> status} from `cargo test` output.

    Test modes (` - should panic`) are dropped from names, and doctests are
    keyed without their `(line N)` so line shifts don't change identity; the
    worst status among an item's doctests wins.
    """
    out: dict[str, str] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        m = _CARGO_TEST_RE.match(raw)
        if not m:
            continue
        name = _CARGO_MODE_RE.sub("", m.group("name")).rstrip()
        status = _CARGO_STATUS[m.group("status")]
        doctest = _CARGO_DOCTEST_RE.match(name)
        if doctest:
            item = doctest.group("item")
            name = f"{doctest.group('file')} - {item}" if item else doctest.group("file")
            if name in out and _CARGO_DOCTEST_RANK[out[name]] > _CARGO_DOCTEST_RANK[status]:
                continue
        _record_worst(out, name, status)
    return out


# Keep these patterns in sync with log_parsers/cargo_parser.py.
_NEXTEST_RE = re.compile(
    r"^\s*(?P<status>[A-Z][A-Z0-9 +/-]*?) \[\s*(?:(?P<running>>)?\s*\d+(?:\.\d+)?s)?\s*\] "
    r"(?:\[\d+/\d+\] )?(?:\((?:\s*\d+/\d+|[-─]+)\) )?(?P<name>\S+ \S.*?)\s*$"
)
_NEXTEST_PASSED_RE = re.compile(
    r"PASS|LEAK|TIMEOUT-PASS|SLOW|SLOW \+ LEAK|SLOW\+TMPASS|FLAKY \d+/\d+"
    r"|TRY \d+ (?:PASS|LEAK|TMPASS)"
)
_NEXTEST_FAILED_RE = re.compile(
    r"FAIL|FAIL \+ LEAK|LEAK-FAIL|TIMEOUT|XFAIL|ABORT|ABORT SIG \d+|SIG[A-Z0-9]+|FLKY-FL \d+/\d+"
    r"|TRY \d+ (?!START$|SLOW$|TRMNTG$)(?:[A-Z0-9+]+|SIG \d+)"
)


def parse_cargo_nextest(log: str) -> dict[str, str]:
    """{"binary-id test::name" -> status} from `cargo nextest run` output.

    Lines still in progress (`SLOW [> 60.000s]`) are skipped; on retries the
    last line for a test wins.
    """
    out: dict[str, str] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        m = _NEXTEST_RE.match(_ANSI_RE.sub("", raw))
        if not m or m.group("running"):
            continue
        status = m.group("status")
        if status == "SKIP":
            out[m.group("name")] = SKIPPED
        elif _NEXTEST_PASSED_RE.fullmatch(status):
            out[m.group("name")] = PASSED
        elif _NEXTEST_FAILED_RE.fullmatch(status):
            out[m.group("name")] = FAILED
    return out


_JEST_FILE_RE = re.compile(r"^ ?(?:PASS|FAIL)\s+(?:\S+\s+)*?(?P<path>\S+\.[mc]?[jt]sx?)(?=\s|$)")
_JEST_FILE_MARKER_RE = re.compile(_JEST_FILE_RE.pattern, re.MULTILINE)
_JEST_TEST_RE = re.compile(
    r"^(?P<indent>\s*)(?P<glyph>✓|√|✕|×|✗|○|◯)\s+(?P<name>.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"
)
_JEST_GLYPH = {
    "✓": PASSED,
    "√": PASSED,
    "✕": FAILED,
    "×": FAILED,
    "✗": FAILED,
    "○": SKIPPED,
    "◯": SKIPPED,
}
# Keep these patterns in sync with log_parsers/jest_parser.py.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VITEST_MARKER_RE = re.compile(r"^\s*(?:RUN\s+v\d+\.\d+|Test Files\s+\d)", re.MULTILINE)
_VITEST_TEST_RE = re.compile(
    r"^\s*(?P<glyph>[✓×↓□]) (?:\|(?P<project>[^|]+)\| | (?P<label>\S+)  )?"
    r"(?P<name>\S+ > .+?)(?P<duration> \d{1,9}ms)?"
    r"(?: \(retry x\d{1,9}\))?(?: \(repeat x\d{1,9}\))?(?: \d{1,9} MB heap used)?(?: \[[^\[\]]*\])?$"
)
# ✔ is log-symbols' check mark, which jest never prints; √ is ambiguous (both
# use it on Windows), so a √-only log is recognized by mocha's epilogue.
_MOCHA_MARKER_RE = re.compile(r"^\s*(?:\d+ passing \(|✔ )", re.MULTILINE)
_MOCHA_EPILOGUE_RE = re.compile(r"^\s*\d+ passing \(")
_MOCHA_FAILING_RE = re.compile(r"^\s*(?P<count>\d+) failing\b")
_MOCHA_TEST_RE = re.compile(
    r"^(?P<indent>\s*)(?:(?P<passed>[✔✓√])|(?P<failed>\d+\))|(?P<pending>-))\s"
    r"(?P<name>\S.*?)(?:\s\(\d{1,9}\s?ms\))?$"
)
_VITEST_STATUS = {"✓": PASSED, "×": FAILED, "↓": SKIPPED, "□": SKIPPED}


def _parse_mocha(log: str) -> dict[str, str]:
    """{test_name -> status} from mocha's spec reporter (no file header)."""
    out: dict[str, str] = {}
    suites: list[tuple[int, str]] = []
    in_epilogue = False
    failures_left = 0
    for raw in log.split("\n"):
        line = raw.rstrip()
        stripped = line.lstrip()
        if not stripped:
            continue
        indent = len(line) - len(stripped)
        if _MOCHA_EPILOGUE_RE.match(line):
            in_epilogue, failures_left = True, 0
            continue
        if in_epilogue:
            failing = _MOCHA_FAILING_RE.match(line)
            if failing:
                failures_left = int(failing.group("count"))
                continue
        m = _MOCHA_TEST_RE.match(line)
        if m:
            if in_epilogue:
                if m.group("failed") and indent == 2 and failures_left > 0:
                    failures_left -= 1
                    continue
                in_epilogue = False
            status = PASSED if m.group("passed") else FAILED if m.group("failed") else SKIPPED
            describes = [title for ind, title in suites if ind < indent]
            _record_worst(out, " > ".join([*describes, m.group("name")]), status)
            continue
        if indent >= 2:
            suites = [(i, t) for i, t in suites if i < indent]
            suites.append((indent, stripped))
    return out


def _parse_vitest(log: str) -> dict[str, str]:
    """{test_name -> status} from vitest `--reporter=verbose` lines only."""
    out: dict[str, str] = {}
    for raw in log.split("\n"):
        m = _VITEST_TEST_RE.match(raw.rstrip())
        if not m:
            continue
        glyph, name = m.group("glyph"), m.group("name")
        if glyph in "↓□" and m.group("duration"):
            name += m.group("duration")
        project = m.group("project") or m.group("label")
        _record_worst(out, f"|{project}| {name}" if project else name, _VITEST_STATUS[glyph])
    return out


def parse_jest(log: str) -> dict[str, str]:
    """{test_name -> status} from Jest / Mocha / Vitest output."""
    out: dict[str, str] = {}
    if not log:
        return out
    log = _ANSI_RE.sub("", log)
    if _VITEST_MARKER_RE.search(log):
        return _parse_vitest(log)
    if _MOCHA_MARKER_RE.search(log) and not _JEST_FILE_MARKER_RE.search(log):
        return _parse_mocha(log)
    current_file: str | None = None
    describe_stack: list[tuple[int, str]] = []
    last_test_indent: int | None = None
    for raw in log.split("\n"):
        line = raw.rstrip()
        if not line:
            continue
        m = _JEST_FILE_RE.match(line)
        if m:
            current_file = m.group("path")
            describe_stack = []
            last_test_indent = None
            continue
        m = _JEST_TEST_RE.match(line)
        if m:
            indent = len(m.group("indent"))
            name = re.sub(r"^(?:skipped|todo):\s*", "", m.group("name").strip())
            describes = [d for ind, d in describe_stack if ind < indent]
            parts = ([current_file] if current_file else []) + describes + [name]
            _record_worst(out, " > ".join(parts), _JEST_GLYPH[m.group("glyph")])
            last_test_indent = indent
            continue
        stripped = line.lstrip()
        if not stripped or stripped.startswith(
            ("Tests:", "Test Suites:", "Snapshots:", "Time:", "Ran all", "●", "→", "✗:")
        ):
            continue
        indent_here = len(line) - len(stripped)
        if current_file and (last_test_indent is None or indent_here < last_test_indent):
            describe_stack = [(i, d) for i, d in describe_stack if i < indent_here]
            describe_stack.append((indent_here, stripped))
    return out


def _detect_runner(test_cmds: str) -> str:
    joined = test_cmds.lower()
    if _PYTEST_RUNNER_RE.search(joined):
        return "pytest"
    if re.search(r"\bunittest\b|manage\.py\s+test\b|runtests\.py", joined):
        return "unittest"
    if re.search(r"\bgo\s+test\b", joined):
        return "go"
    if re.search(r"\bcargo\s+nextest\b", joined):
        return "nextest"
    if re.search(r"\bcargo\s+test\b", joined):
        return "cargo"
    if any(k in joined for k in ("jest", "mocha", "vitest", "npm test", "yarn test", "pnpm test")):
        return "jest"
    return "unknown"


# Lines longer than this are dropped before any parser sees them: no runner
# prints a test result that long, and it bounds what a regex can cost.
MAX_LINE = 4096


def drop_long_lines(log: str) -> str:
    """`log` with every line over MAX_LINE characters emptied."""
    return "\n".join(line if len(line) <= MAX_LINE else "" for line in log.split("\n"))


def parse_logs(runner: str, log: str) -> dict[str, str]:
    """Dispatch to the right per-runner parser. Empty dict if unknown."""
    log = drop_long_lines(log)
    if runner == "pytest":
        return parse_pytest(log)
    if runner == "unittest":
        return parse_unittest(log)
    if runner == "go":
        return parse_go_test(log)
    if runner == "cargo":
        return parse_cargo_test(log)
    if runner == "nextest":
        return parse_cargo_nextest(log)
    if runner == "jest":
        return parse_jest(log)
    return {}


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
def _has_go_parent(name: str, status_map: dict[str, str]) -> bool:
    """Return True if a Go test has an ancestor represented in the status map."""
    parts = name.split("/")

    for i in range(1, len(parts)):
        parent = "/".join(parts[:i])
        if status_map.get(parent) == FAILED:
            return True

    return False


def grade(
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    status_map: dict[str, str],
    runner: str | None = None,
) -> dict:
    """Compute the graded reward + strict resolved bool from a status map.

    f2p_rate   = (# F2P now PASSED) / (# F2P)
    p2p_rate   = (# P2P still PASSED) / (# P2P)   [1.0 when no P2P]
    reward     = f2p_rate * p2p_rate                       (dense training signal)
    resolved   = all F2P pass AND all P2P pass   (SWE-bench TRACKED resolution —
                 the gold patch always satisfies this, preserving the oracle
                 invariant)

    Two distinct EVAL signals (see the audit's "tracked vs command resolved"):
      * `resolved`         — tracked resolution (above). Gold patch -> True.
      * `command_resolved` — stricter: tracked resolution AND the selected test
                             command had NO failures outside F2P/P2P AND exit
                             code 0. Computed in main() where the exit code is
                             known. A benchmark that wants "the whole command
                             passed cleanly" gates on this; SWE-bench-style
                             scoring uses `resolved`.

    `untracked_failed` are FAILED tests in the run that are neither F2P nor
    P2P (e.g. always-failing/flaky tests pulled in by running a whole test
    file). They don't change the graded `reward` or tracked `resolved`, but
    they block `command_resolved` and are recorded for transparency.
    """
    f2p_total = len(fail_to_pass)
    p2p_total = len(pass_to_pass)
    f2p_set = set(fail_to_pass)
    p2p_set = set(pass_to_pass)
    f2p_passed = sum(1 for t in fail_to_pass if status_map.get(t) == PASSED)
    p2p_passed = sum(1 for t in pass_to_pass if status_map.get(t) == PASSED)
    # Tests that should have stayed green but regressed (PASS->not-pass).
    regressions = [t for t in pass_to_pass if status_map.get(t) != PASSED]

    # FAILED tests outside the tracked sets — the selected command isn't clean.
    if runner == "go":
        failed_tests = {
            t
            for t, s in status_map.items()
            if s == FAILED and t not in f2p_set and t not in p2p_set
        }

        untracked_failed = sorted(t for t in failed_tests if not _has_go_parent(t, status_map))
    else:
        untracked_failed = sorted(
            t
            for t, s in status_map.items()
            if s == FAILED and t not in f2p_set and t not in p2p_set
        )

    f2p_rate = (f2p_passed / f2p_total) if f2p_total else 0.0
    p2p_rate = (p2p_passed / p2p_total) if p2p_total else 1.0
    reward = f2p_rate * p2p_rate
    resolved = f2p_total > 0 and f2p_passed == f2p_total and p2p_passed == p2p_total

    return {
        "reward": round(max(0.0, min(1.0, reward)), 6),
        "resolved": resolved,
        "f2p_total": f2p_total,
        "f2p_passed": f2p_passed,
        "f2p_rate": round(f2p_rate, 6),
        "p2p_total": p2p_total,
        "p2p_passed": p2p_passed,
        "p2p_rate": round(p2p_rate, 6),
        "regressions": sorted(regressions),
        "untracked_failed_count": len(untracked_failed),
        "untracked_failed": untracked_failed[:20],  # cap the list
    }


# ---------------------------------------------------------------------------
# Mutant runs (see repo2rlenv.pipelines._mutants)
# ---------------------------------------------------------------------------


def mutant_verdict(
    ids: list[str],
    run_a: dict[str, str],
    run_b: dict[str, str],
    tracked: list[str],
    *,
    exit_code_b: int,
    exit_nonzero: bool,
) -> str | None:
    """Return the signal the two runs raise, or None.

    Run A is the hidden suite as is; run B is the same tree with each mutated
    test contradicting the original (changed expected value; same test ID).
    No honest submission passes both a test and its
    mutant, so only tests run A reports as passed count ("claimed"): a wrong
    submission that fails a test may well pass its mutant. These mark the run
    as tampered:
      * `mutant_passed`        - a claimed test's mutant is passed or skipped in B;
      * `mutant_missing`       - a claimed test's mutant is absent from B while
                                 tracked tests pass there;
      * `mutant_run_exit_zero` - B exited 0 although a claimed test's mutant
                                 must fail;
    These score 0 without the tampered label (`MISMATCH_SIGNALS`): flaky tests
    are dropped at generation, so they most likely mean a forger, but a flake
    or an honest solution slower than the reference under load can raise them:
      * `status_mismatch`      - another tracked test (a decoy among them)
                                 differs between A and B;
      * `mutant_timeout`       - B ran past its time limit after A claimed a
                                 mutated test (decided in `main`).
    Tampering signals are checked first, so a run that raises both is tampered.
    """
    mutated = set(ids)
    claimed = [test for test in ids if run_a.get(test) == PASSED]
    for test in ids:
        status = run_b.get(test)
        if test in claimed and status is not None and status not in (FAILED, ERROR):
            return "mutant_passed"
        # A wrong implementation can coincidentally return the mutant's value.
        # Unless it also claimed the original passed, there is no contradiction
        # and no evidence of forgery; preserve its ordinary partial reward.
    if any(test not in run_b for test in claimed) and any(
        run_b.get(test) == PASSED for test in tracked
    ):
        return "mutant_missing"
    if exit_nonzero and exit_code_b == 0 and claimed:
        return "mutant_run_exit_zero"
    for test in tracked:
        if test not in mutated and run_a.get(test) != run_b.get(test):
            return "status_mismatch"
    return None


# Signals a flaky suite could also raise: graded 0, but not labelled tampered.
# Leaving them ungraded would let a forger that fails everything in run B drop
# its own rollout instead of taking the 0.
MISMATCH_SIGNALS = frozenset({"status_mismatch", "mutant_timeout"})


def _read_mutants(path: str) -> tuple[list[str], bool]:
    """The mutated test IDs from /tests/mutants.json (root-only, written at
    generation); an unreadable spec is a broken task, not a skipped check."""
    if not path:
        return [], False
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
        ids = [str(x) for x in spec.get("ids", [])]
    except (OSError, ValueError, AttributeError) as exc:
        raise GraderInputError(f"{path}: {exc}") from exc
    return ids, bool(spec.get("exit_nonzero")) and bool(ids)


def _mutant_check(args, ids: list[str], exit_nonzero: bool, run_a: dict, run_b, tracked) -> dict:
    """The tamper check's fields for reward-details.json (empty without mutants)."""
    if not ids:
        return {"tamper_check": "unavailable"}
    out: dict = {}
    signal = None
    if args.timed_out:
        pass  # run A is a graded 0 anyway; nothing for run B to check
    elif args.mutant_skipped:
        pass  # run A passed no mutated test, so run B could not change the verdict
    elif args.mutant_timed_out:
        # Run B differs from A by a few assertions, so it takes about as long;
        # hanging it must not be a way out of the check.
        if any(run_a.get(test) == PASSED for test in ids):
            signal = "mutant_timeout"
        out["mutant_exit_code"] = args.mutant_exit_code
    else:
        signal = mutant_verdict(
            ids,
            run_a,
            run_b(),
            tracked,
            exit_code_b=args.mutant_exit_code,
            exit_nonzero=exit_nonzero,
        )
        out["mutant_exit_code"] = args.mutant_exit_code
    seconds_b = None if args.mutant_skipped else _read_seconds(args.mutant_seconds)
    if seconds_b is not None:
        out["mutant_run_seconds"] = seconds_b
    if args.timed_out or (args.mutant_skipped and not signal):
        out["tamper_check"] = "not_needed"
    elif signal in MISMATCH_SIGNALS:
        out["tamper_check"] = "inconclusive"
    else:
        out["tamper_check"] = "tripped" if signal else "passed"
    if signal:
        out.update(
            {
                "reward": 0.0,
                "resolved": False,
                "command_resolved": False,
                "parse_status": signal if signal in MISMATCH_SIGNALS else "tampered",
                "tamper_signal": signal,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Entry point (invoked by tests/test.sh inside the container)
# ---------------------------------------------------------------------------

_MAX_READ = 64 * 1024 * 1024


def _read_untrusted(path: str, limit: int = _MAX_READ) -> bytes:
    """Read a file test code could have touched: no links, no FIFOs, bounded.

    Called only after every process of the test user is gone, so nothing can
    change the file while it is read.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(descriptor, "rb") as f:
            descriptor = -1
            data = f.read(limit + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > limit:
        raise ValueError("file over the size limit")
    return data


def _read_seconds(path: str) -> float | None:
    """A wall time the root launcher wrote; None when absent or unreadable."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            value = float(f.read(64).strip())
    except (OSError, ValueError):
        return None
    return round(value, 3) if value >= 0 else None


def _load_test_results():
    """`parse_junit` from the repository recipe's `test_results.py`.

    The task ships it next to this file in /tests; in the source tree it is
    `repo2rlenv.quality.test_results`.
    """
    import importlib.util

    shipped = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_results.py")
    if os.path.isfile(shipped):
        spec = importlib.util.spec_from_file_location("r2e_test_results", shipped)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolve the module by name
        spec.loader.exec_module(module)
        return module.parse_junit
    from repo2rlenv.quality.test_results import parse_junit

    return parse_junit


def junit_statuses(report: bytes, exit_code: int) -> dict[str, str]:
    """Upper-cased statuses from a JUnit report; {} when it can't be parsed."""
    try:
        results = _load_test_results()(report.decode("utf-8", "replace"), returncode=exit_code)
    except ValueError:
        return {}
    return {name: status.upper() for name, status in results.statuses.items()}


def _read_report(path: str) -> bytes:
    try:
        return _read_untrusted(path, 16 * 1024 * 1024)
    except (OSError, ValueError):
        return b""


def junit_verdict(report: bytes, exit_code: int, expected: list[str]) -> dict:
    """code_instruct / equivalence_tests: grade a JUnit report against `expected`.

    Reward 1.0 iff pytest exited 0 and the report holds exactly the expected
    test IDs (the reference run's), every one passed: a skipped, missing or
    extra test fails, and so does a report that contradicts the exit code. A
    report the submission writes itself must name exactly the hidden tests;
    code under test that reads the test file and forges every one of them is
    in-process forgery, which the mutant check (`mutant_verdict`) catches.
    """
    try:
        results = _load_test_results()(report.decode("utf-8", "replace"), returncode=exit_code)
        statuses = {name: status.upper() for name, status in results.statuses.items()}
        parse_status = "ok"
    except ValueError as exc:
        statuses, parse_status = {}, f"invalid_report: {exc}"[:200]
    missing = sorted(set(expected) - set(statuses))
    extra = sorted(set(statuses) - set(expected)) if expected else []
    # Without an expected list (generated with --skip-validation) the report is
    # trusted as is: every test it lists passed and pytest exited 0.
    passed = (
        exit_code == 0
        and bool(statuses)
        and not missing
        and not extra
        and all(status == PASSED for status in statuses.values())
    )
    return {
        "reward": 1.0 if passed else 0.0,
        "eval_trustworthy": bool(expected),
        "resolved": passed,
        "command_resolved": passed,
        "parse_status": parse_status,
        "tests_expected": len(expected),
        "tests_parsed": len(statuses),
        "tests_passed": sum(1 for s in statuses.values() if s == PASSED),
        "missing": missing[:20],
        "unexpected": extra[:20],
        "not_passed": sorted(t for t, s in statuses.items() if s != PASSED)[:20],
        "exit_code": exit_code,
    }


class GraderInputError(Exception):
    """A file the task ships in root-only /tests is missing or unreadable."""


# Exit status for a broken task (unreadable /tests inputs): the caller leaves the
# run ungraded. Any other failure (a crash or the CPU limit while parsing the
# submission's own output) is graded 0 by the caller.
EXIT_GRADER_INPUT = 3
# CPU seconds the verifier may spend: parsing is linear, so only a pathological
# log printed by the code under test comes near it.
CPU_LIMIT_SEC = 120


def _read_json_list(path: str) -> list[str]:
    """A JSON list of test IDs from /tests (written at generation, root-only)."""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise GraderInputError(f"{path}: {exc}") from exc
    if not isinstance(data, list):
        raise GraderInputError(f"{path}: not a JSON list")
    return [str(x) for x in data]


def _limit_cpu() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SEC, CPU_LIMIT_SEC + 5))
    except (ImportError, ValueError, OSError):
        pass


def _read_text(path: str) -> str:
    try:
        return _read_untrusted(path).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def _log_breakdown(args, status_map: dict[str, str]) -> dict:
    """pr_runtime family: grade the captured log against the F2P/P2P lists."""
    f2p = _read_json_list(args.f2p)
    p2p = _read_json_list(args.p2p)
    runner = args.runner.strip() or _detect_runner(args.test_cmds)
    if not status_map:
        # Unparseable runner output → fall back to the binary exit-code reward
        # (a coarse TRAINING signal) so we never silently zero a real fix on an
        # unrecognized format. But `resolved` is the strict EVAL signal: without
        # parsed per-test status we have NO evidence the declared FAIL_TO_PASS
        # tests passed, so when an F2P oracle exists we must NOT claim resolved.
        # (resolved stays exit-code-based only when there's no declared oracle,
        # e.g. --skip-validation.) The same holds for the reward: generation
        # parsed this runner's output to build the F2P list, so with an oracle
        # declared an unparseable log provides no evidence of passing tests,
        # whether caused by environment failure, parser drift or tampering.
        has_oracle = len(f2p) > 0
        resolved = (args.exit_code == 0) and not has_oracle
        return {
            "reward": 1.0 if resolved else 0.0,
            "resolved": resolved,
            "command_resolved": resolved,
            "parse_status": "fallback_exitcode",
            "eval_trustworthy": not has_oracle,
            "runner": runner,
            "f2p_total": len(f2p),
            "p2p_total": len(p2p),
            "exit_code": args.exit_code,
        }
    breakdown = grade(f2p, p2p, status_map, runner)
    breakdown["parse_status"] = "ok"
    breakdown["runner"] = runner
    breakdown["tests_parsed"] = len(status_map)
    breakdown["exit_code"] = args.exit_code
    # Strict eval signal: tracked resolution AND a clean command (no
    # untracked failures, exit code 0). Benchmarks wanting "the whole test
    # command passed" gate on this; SWE-bench-style scoring uses `resolved`.
    breakdown["command_resolved"] = bool(
        breakdown["resolved"] and breakdown["untracked_failed_count"] == 0 and args.exit_code == 0
    )
    return breakdown


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="pr_runtime graded F2P/P2P verifier")
    p.add_argument("--log", default="", help="captured test-run log file")
    p.add_argument("--f2p", default="", help="JSON file: FAIL_TO_PASS test names")
    p.add_argument("--p2p", default="", help="JSON file: PASS_TO_PASS test names")
    p.add_argument(
        "--runner", default="", help="pytest|unittest|go|cargo|nextest|jest (else auto-detect)"
    )
    p.add_argument("--test-cmds", default="", help="test command string (runner auto-detect)")
    p.add_argument("--exit-code", type=int, default=1, help="test suite exit code (fallback)")
    p.add_argument("--out-dir", default="/logs/verifier", help="where to write reward.{txt,json}")
    p.add_argument("--junit", default="", help="grade this JUnit report instead of the log")
    p.add_argument("--expected", default="", help="JSON list: test IDs the JUnit report must hold")
    p.add_argument("--timed-out", type=int, default=0, help="1 if the test run hit its time limit")
    p.add_argument("--seconds", default="", help="file with the test run's wall time")
    p.add_argument("--mutants", default="", help="JSON mutant spec (tests/mutants.json)")
    p.add_argument("--mutant-log", default="", help="log of the run with mutants (run B)")
    p.add_argument("--mutant-junit", default="", help="JUnit report of run B")
    p.add_argument("--mutant-exit-code", type=int, default=1, help="exit code of run B")
    p.add_argument("--mutant-timed-out", type=int, default=0, help="1 if run B hit its limit")
    p.add_argument("--mutant-skipped", type=int, default=0, help="1 if run B was not needed")
    p.add_argument("--mutant-seconds", default="", help="file with run B's wall time")
    p.add_argument(
        "--claimed",
        action="store_true",
        help="print 1 if run A passed a mutated test (or can't be read), else 0; write nothing",
    )
    args = p.parse_args(argv)
    if argv is None:  # running as the task's verifier, not imported by a test
        _limit_cpu()
    try:
        if args.claimed:
            print(_claimed(args))
            return 0
        breakdown = _breakdown(args)
    except GraderInputError as exc:
        sys.stderr.write(f"R2E verifier: unreadable grader input: {exc}\n")
        return EXIT_GRADER_INPUT
    return _write_verdict(args, breakdown)


def _runner(args) -> str:
    return args.runner.strip() or _detect_runner(args.test_cmds)


def _claimed(args) -> str:
    """Whether run B is needed. A run A that can't be read means "run it":
    skipping is only safe when run A plainly passed no mutated test."""
    ids, _ = _read_mutants(args.mutants)
    if args.junit:
        report = _read_report(args.junit)
        run_a = junit_statuses(report, args.exit_code) if report else {}
    else:
        run_a = parse_logs(_runner(args), _read_text(args.log))
    return "1" if not run_a or any(run_a.get(test) == PASSED for test in ids) else "0"


def _breakdown(args) -> dict:
    ids, exit_nonzero = _read_mutants(args.mutants)
    if args.junit:
        report = _read_report(args.junit)
        expected = _read_json_list(args.expected)
        breakdown = junit_verdict(report, args.exit_code, expected)
        if report:
            # Keep the bytes graded, root-only, for the record.
            os.makedirs(args.out_dir, exist_ok=True)
            path = os.path.join(args.out_dir, "junit.xml")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as f:
                f.write(report)
        run_a = junit_statuses(report, args.exit_code) if report else {}
        tracked = sorted(set(expected or run_a) - set(ids))

        def run_b() -> dict[str, str]:
            data = _read_report(args.mutant_junit)
            return junit_statuses(data, args.mutant_exit_code) if data else {}
    else:
        runner = _runner(args)
        run_a = parse_logs(runner, _read_text(args.log))
        breakdown = _log_breakdown(args, run_a)
        tracked = _read_json_list(args.f2p) + _read_json_list(args.p2p)

        def run_b() -> dict[str, str]:
            return parse_logs(runner, _read_text(args.mutant_log))

    check = _mutant_check(args, ids, exit_nonzero, run_a, run_b, tracked)
    seconds = _read_seconds(args.seconds)
    if seconds and check.get("mutant_run_seconds") is not None:
        check["mutant_time_ratio"] = round(check["mutant_run_seconds"] / seconds, 3)
    breakdown.update(check)
    return breakdown


def _write_verdict(args, breakdown: dict) -> int:
    seconds = _read_seconds(args.seconds)
    if seconds is not None:
        breakdown["run_seconds"] = seconds
    if args.timed_out:
        # The submission controls how long its tests take: a hang is a wrong answer.
        breakdown.update(
            {
                "reward": 0.0,
                "resolved": False,
                "command_resolved": False,
                "parse_status": "test_timeout",
            }
        )
    breakdown["evaluation_status"] = "graded"
    reward = breakdown["reward"]

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "reward.txt"), "w", encoding="utf-8") as f:
        f.write(f"{reward:.6f}\n")
    with open(os.path.join(args.out_dir, "reward-details.json"), "w", encoding="utf-8") as f:
        json.dump(breakdown, f, indent=2)

    print(json.dumps(breakdown, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "MISMATCH_SIGNALS",
    "grade",
    "junit_statuses",
    "junit_verdict",
    "main",
    "mutant_verdict",
    "parse_cargo_nextest",
    "parse_cargo_test",
    "parse_go_test",
    "parse_jest",
    "parse_logs",
    "parse_pytest",
    "parse_unittest",
]
