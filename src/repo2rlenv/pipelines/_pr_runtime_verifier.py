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
  * /logs/verifier/reward.json — carries the strict SWE-bench
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
statuses (unrecognized runner output), fall back to the exit-code
reward (1.0 if the suite exited 0, else 0.0) and stamp
``parse_status="fallback_exitcode"`` — never crash, never silently
zero out a real fix.

Pure stdlib — uses only ``argparse``, ``json``, ``os``, ``re``, ``sys``.
The 4 per-runner parsers are condensed ports of
``repo2rlenv.log_parsers.*`` kept in lockstep via the unit tests under
``tests/test_pr_runtime_verifier.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
_PYTEST_VERBOSE_RE = re.compile(r"^(?P<name>\S+)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR)\b")


def parse_pytest(log: str) -> dict[str, str]:
    """{test_name -> status} from pytest output (verbose or summary)."""
    out: dict[str, str] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        line = raw.strip()
        if not line:
            continue
        # Summary lines (STATUS first) — checked before verbose so a
        # "PASSED tests/foo.py::test_a" line isn't misread as name=PASSED.
        leading = None
        for st in _PYTEST_STATUSES:
            if line.startswith(st + " ") or line == st:
                leading = st
                break
        if leading is not None:
            work = line
            if leading == FAILED and " - " in work:
                work = work.split(" - ", 1)[0]
            tokens = work.split()
            if len(tokens) < 2:
                continue
            name = tokens[1]
            if name.startswith("[") and name.endswith("]"):  # SKIPPED [N] file:line
                if len(tokens) < 3:
                    continue
                name = tokens[2]
            out[name] = leading
            continue
        # Verbose progress (NAME first, STATUS after)
        m = _PYTEST_VERBOSE_RE.match(line)
        if m:
            name = m.group("name")
            if "::" in name or name.endswith(".py"):
                out[name] = m.group("status")
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
            out[m.group("name")] = _GO_STATUS[m.group("status")]
    return out


_CARGO_TEST_RE = re.compile(r"^test\s+(?P<name>\S+)\s+\.\.\.\s+(?P<status>ok|FAILED|ignored)\b")
_CARGO_STATUS = {"ok": PASSED, "FAILED": FAILED, "ignored": SKIPPED}


def parse_cargo_test(log: str) -> dict[str, str]:
    """{test_name -> status} from `cargo test` output."""
    out: dict[str, str] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        m = _CARGO_TEST_RE.match(raw)
        if m:
            out[m.group("name")] = _CARGO_STATUS[m.group("status")]
    return out


_JEST_FILE_RE = re.compile(r"^(?:PASS|FAIL)\s+(?P<path>\S+\.(?:ts|tsx|js|jsx|mjs|cjs))\b")
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


def parse_jest(log: str) -> dict[str, str]:
    """{test_name -> status} from Jest / Mocha / Vitest output."""
    out: dict[str, str] = {}
    if not log:
        return out
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
            out[" > ".join(parts)] = _JEST_GLYPH[m.group("glyph")]
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
    if "pytest" in joined:
        return "pytest"
    if re.search(r"\bgo\s+test\b", joined):
        return "go"
    if re.search(r"\bcargo\s+test\b", joined):
        return "cargo"
    if any(k in joined for k in ("jest", "mocha", "vitest", "npm test", "yarn test", "pnpm test")):
        return "jest"
    return "unknown"


def parse_logs(runner: str, log: str) -> dict[str, str]:
    """Dispatch to the right per-runner parser. Empty dict if unknown."""
    if runner == "pytest":
        return parse_pytest(log)
    if runner == "go":
        return parse_go_test(log)
    if runner == "cargo":
        return parse_cargo_test(log)
    if runner == "jest":
        return parse_jest(log)
    return {}


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def grade(
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    status_map: dict[str, str],
) -> dict:
    """Compute the graded reward + strict resolved bool from a status map.

    f2p_rate   = (# F2P now PASSED) / (# F2P)
    p2p_rate   = (# P2P still PASSED) / (# P2P)   [1.0 when no P2P]
    reward     = f2p_rate * p2p_rate
    resolved   = all F2P pass AND all P2P pass    (strict SWE-bench)
    """
    f2p_total = len(fail_to_pass)
    p2p_total = len(pass_to_pass)
    f2p_passed = sum(1 for t in fail_to_pass if status_map.get(t) == PASSED)
    p2p_passed = sum(1 for t in pass_to_pass if status_map.get(t) == PASSED)
    # Tests that should have stayed green but regressed (PASS->not-pass).
    regressions = [t for t in pass_to_pass if status_map.get(t) != PASSED]

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
    }


# ---------------------------------------------------------------------------
# Entry point (invoked by tests/test.sh inside the container)
# ---------------------------------------------------------------------------


def _read_json_list(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="pr_runtime graded F2P/P2P verifier")
    p.add_argument("--log", required=True, help="captured test-run log file")
    p.add_argument("--f2p", required=True, help="JSON file: FAIL_TO_PASS test names")
    p.add_argument("--p2p", required=True, help="JSON file: PASS_TO_PASS test names")
    p.add_argument("--runner", default="", help="pytest|go|cargo|jest (else auto-detect)")
    p.add_argument("--test-cmds", default="", help="test command string (runner auto-detect)")
    p.add_argument("--exit-code", type=int, default=1, help="test suite exit code (fallback)")
    p.add_argument("--out-dir", default="/logs/verifier", help="where to write reward.{txt,json}")
    args = p.parse_args(argv)

    log = _read_text(args.log)
    f2p = _read_json_list(args.f2p)
    p2p = _read_json_list(args.p2p)
    runner = args.runner.strip() or _detect_runner(args.test_cmds)

    status_map = parse_logs(runner, log)

    if not status_map:
        # Unparseable runner output → fall back to the binary exit-code reward
        # so we never silently zero a real fix on an unrecognized format.
        reward = 1.0 if args.exit_code == 0 else 0.0
        breakdown = {
            "reward": reward,
            "resolved": args.exit_code == 0,
            "parse_status": "fallback_exitcode",
            "runner": runner,
            "f2p_total": len(f2p),
            "p2p_total": len(p2p),
            "exit_code": args.exit_code,
        }
    else:
        breakdown = grade(f2p, p2p, status_map)
        breakdown["parse_status"] = "ok"
        breakdown["runner"] = runner
        breakdown["tests_parsed"] = len(status_map)
        reward = breakdown["reward"]

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "reward.txt"), "w", encoding="utf-8") as f:
        f.write(f"{reward:.6f}\n")
    with open(os.path.join(args.out_dir, "reward.json"), "w", encoding="utf-8") as f:
        json.dump(breakdown, f, indent=2)

    print(json.dumps(breakdown, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "grade",
    "main",
    "parse_cargo_test",
    "parse_go_test",
    "parse_jest",
    "parse_logs",
    "parse_pytest",
]
