"""TAP output (QUnit, tape, node-tap, `--reporter tap`) must parse in both parsers."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from repo2rlenv.log_parsers import _detect_runner, parse_logs
from repo2rlenv.log_parsers.tap_parser import parse_tap
from repo2rlenv.pipelines import _pr_runtime_verifier as verifier


@pytest.fixture(params=[parse_tap, verifier.parse_tap], ids=["canonical", "standalone"])
def parser(request):
    return request.param


# Captured from `qunit t/sample.js` (qunit 2.26.0): every status QUnit can report,
# a failure with a YAML block, and a name containing `#`, `[]` and `()`.
_QUNIT = """\
TAP version 13
ok 1 add and subtract > add short reverse args
not ok 2 add and subtract > broken one
  ---
  message: wrong
  severity: failed
  actual  : 2
  expected: 3
  stack: |
        at Object.<anonymous> (/tmp/t/sample.js:3:55)
  ...
ok 3 add and subtract > skipped one # SKIP
not ok 4 add and subtract > todo one # TODO
  ---
  message: failed
  severity: todo
  ...
ok 5 add and subtract > weird name: (a) [b] # not a directive
ok 6 top level
1..6
# pass 3
# skip 1
# todo 1
# fail 1
"""


def test_qunit_statuses(parser):
    assert parser(_QUNIT) == {
        "add and subtract > add short reverse args": "PASSED",
        "add and subtract > broken one": "FAILED",
        "add and subtract > skipped one": "SKIPPED",
        # A `# TODO` that fails is an expected failure: QUnit exits 0 on it.
        "add and subtract > todo one": "PASSED",
        "add and subtract > weird name: (a) [b] # not a directive": "PASSED",
        "top level": "PASSED",
    }


def test_yaml_blocks_plan_and_summary_are_not_tests(parser):
    names = set(parser(_QUNIT))
    assert not any(n.startswith(("message", "severity", "1..", "# ")) for n in names)
    assert len(names) == 6


def test_code_under_test_cannot_upgrade_a_failure(parser):
    # The code under test prints `ok 2 ...` after the runner's real `not ok 2 ...`.
    log = "TAP version 13\nnot ok 1 suite > t\nok 2 suite > t\n1..2\n"
    assert parser(log) == {"suite > t": "FAILED"}


def test_unnamed_points_get_a_numbered_name(parser):
    assert parser("TAP version 13\nok 1\nnot ok 2\n1..2\n") == {
        "test 1": "PASSED",
        "test 2": "FAILED",
    }


def test_dash_separator_and_no_number(parser):
    assert parser("ok 1 - Input file opened\nnot ok - no number\n") == {
        "Input file opened": "PASSED",
        "no number": "FAILED",
    }


def test_indented_subtests_are_ignored(parser):
    log = "TAP version 13\n    ok 1 - nested subtest\nok 1 parent\n"
    assert parser(log) == {"parent": "PASSED"}


def test_ansi_colors_are_stripped(parser):
    assert parser("\x1b[32mTAP version 13\x1b[0m\n\x1b[32mok 1 colored\x1b[0m\n") == {
        "colored": "PASSED"
    }


def test_empty_and_non_tap_logs(parser):
    assert parser("") == {}
    assert parser("Global summary:\n17 tests, 154 assertions, 0 failed tests\n") == {}


def test_go_package_lines_never_reach_the_tap_parser():
    # `go test` prints `ok  \tpkg\t0.1s`; dispatch only picks TAP for a TAP command
    # or a `TAP version` header, so a Go run is read by the Go parser.
    go_log = "ok  \tgithub.com/x/y\t0.012s\n"
    assert _detect_runner(["go test ./..."]) == "go"
    assert "TAP version" not in go_log
    assert parse_logs(["go test ./..."], go_log) != parse_tap(go_log)


def test_no_tests_run_is_a_single_failed_point(parser):
    log = "TAP version 13\nnot ok 1 global failure\n  ---\n  message: No tests were run.\n  ...\n1..1\n"
    assert parser(log) == {"global failure": "FAILED"}


@pytest.mark.parametrize(
    "cmd",
    [
        "npx qunit build/qunit/test/**/*.js",
        "node node_modules/qunit/bin/qunit t/sample.js",
        "tape test/*.js",
        "npx mocha --reporter tap",
        "mocha -R tap",
        "node --test --test-reporter=tap",
        "vitest --reporter=tap",
    ],
)
def test_tap_commands_are_detected_by_both_dispatchers(cmd):
    assert _detect_runner([cmd]) == "tap"
    assert verifier._detect_runner(cmd) == "tap"
    assert parse_logs([cmd], _QUNIT) == parse_tap(_QUNIT)
    assert verifier.parse_logs(verifier._detect_runner(cmd), _QUNIT) == verifier.parse_tap(_QUNIT)


@pytest.mark.parametrize(
    ("cmd", "runner"),
    [
        ("npx jest --verbose", "jest"),
        ("npx mocha --reporter spec", "jest"),
        ("npm test", "jest"),
        ("pytest -v", "pytest"),
        ("go test ./...", "go"),
    ],
)
def test_other_runners_are_not_taken_for_tap(cmd, runner):
    assert _detect_runner([cmd]) == runner
    assert verifier._detect_runner(cmd) == runner


def test_wrapper_command_with_tap_header_falls_back_to_tap():
    # `npm test` hides the runner; jest's parser finds nothing, the TAP header decides.
    assert parse_logs(["npm test"], _QUNIT) == parse_tap(_QUNIT)
    assert verifier.parse_logs("jest", _QUNIT) == verifier.parse_tap(_QUNIT)


def test_jest_output_is_unchanged_by_the_tap_fallback():
    jest_log = "PASS src/foo.test.ts\n  Foo\n    ✓ returns 200 (4 ms)\n"
    assert parse_logs(["npm test"], jest_log) == {"src/foo.test.ts > Foo > returns 200": "PASSED"}


def test_summary_only_output_is_still_unparseable():
    # moment's scripts/test.js: the case that made bootstrap reject the command.
    log = "\nGlobal summary:\n17 tests, 154 assertions, 0 failed tests, 0 failed assertions, 28ms\n"
    assert parse_logs(["node scripts/test.js"], log, language="node") == {}


def test_runtime_normalization_leaves_tap_commands_alone():
    from repo2rlenv.pipelines.pr_runtime import normalize_test_cmds_for_runtime

    cmd = "cd /workspace && node scripts/build.js && npx qunit build/qunit/test/**/*.js"
    assert normalize_test_cmds_for_runtime([cmd]) == [cmd]


def test_long_lines_do_not_stall_parsing():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from repo2rlenv.log_parsers.tap_parser import parse_tap\n"
            "from repo2rlenv.pipelines._pr_runtime_verifier import parse_tap as standalone\n"
            "noise = ['ok ' + '1 ' * 200000, 'not ok ' + 'x' * 200000 + ' # ' * 50000]\n"
            "log = '\\n'.join(noise) + '\\nok 7 real test\\n'\n"
            "for parser in (parse_tap, standalone):\n"
            "    assert parser(log)['real test'] == 'PASSED'\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_verifier_grades_a_tap_log_end_to_end(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(_QUNIT, encoding="utf-8")
    f2p = tmp_path / "f2p.json"
    f2p.write_text(json.dumps(["add and subtract > add short reverse args"]), encoding="utf-8")
    p2p = tmp_path / "p2p.json"
    p2p.write_text(json.dumps(["top level"]), encoding="utf-8")
    out = tmp_path / "out"

    code = verifier.main(
        [
            "--log",
            str(log),
            "--f2p",
            str(f2p),
            "--p2p",
            str(p2p),
            "--test-cmds",
            "npx qunit build/**/*.js",
            "--exit-code",
            "1",
            "--out-dir",
            str(out),
        ]
    )
    assert code == 0
    assert float((out / "reward.txt").read_text()) == 1.0
