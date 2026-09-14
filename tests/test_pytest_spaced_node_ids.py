"""Regression coverage for pytest node IDs with spaces and non-default console styles."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repo2rlenv.log_parsers.pytest_parser import parse_pytest as parse_canonical_pytest
from repo2rlenv.pipelines import _pr_runtime_verifier as runtime_verifier
from repo2rlenv.pipelines._pr_runtime_verifier import (
    grade,
    parse_pytest as parse_runtime_pytest,
)


@pytest.fixture(params=[parse_canonical_pytest, parse_runtime_pytest], ids=["canonical", "runtime"])
def parser(request):
    return request.param


@pytest.mark.parametrize(
    "parameter",
    [
        "1 + 1",
        "1  +  1",
        "left - right",
        "a PASSED b FAILED c SKIPPED d ERROR e",
        "PASSED [ 50%] then FAILED",
        "nested [value] with spaces",
        "unmatched [ bracket",
        "unmatched ] bracket",
    ],
)
@pytest.mark.parametrize("style", ["verbose", "progress", "summary"])
def test_preserves_complete_parameter_id(parser, parameter, style):
    node = f"tests/test_calc.py::test_eval[{parameter}]"
    if style == "summary":
        log = f"FAILED {node} - AssertionError: [message] - more detail\n"
    elif style == "progress":
        log = f"{node} FAILED [ 50%]\n"
    else:
        log = f"{node} FAILED\n"

    assert parser(log) == {node: "FAILED"}


@pytest.mark.parametrize("status", ["PASSED", "FAILED", "SKIPPED", "ERROR"])
def test_all_statuses_preserve_spaced_node_ids(parser, status):
    node = "tests/test_calc.py::test_eval[1 + 1]"
    assert parser(f"{node} {status} [100%]\n") == {node: status}
    assert parser(f"{status} {node}\n") == {node: status}


def test_verbose_skip_reason_is_preserved(parser):
    node = "test_cases.py::test_skip[x]"
    log = f"{node} SKIPPED (needs optional dep)          [ 94%]\n"
    assert parser(log) == {node: "SKIPPED"}


def test_folded_skip_summary_records_location_only(parser):
    assert parser("SKIPPED [2] test_cases.py:26: needs optional dep\n") == {
        "test_cases.py:26:": "SKIPPED"
    }


def test_bare_folded_skip_count_is_ignored(parser):
    assert parser("SKIPPED [2]\n") == {}


def test_unbalanced_open_bracket_does_not_keep_failure_diagnostic(parser):
    node = "test_cases.py::test_fail[unmatched [ bracket]"
    log = f"FAILED {node} - AssertionError: boom [unmatched [ bracket] - detail\n"
    assert parser(log) == {node: "FAILED"}


@pytest.mark.parametrize(
    "node",
    [
        "tests/a.py::test_x[left - right]",
        "tests/a.py::TestClass::test_x[a - b]",
    ],
)
def test_diagnostic_double_colon_does_not_truncate_node_id(parser, node):
    log = f"FAILED {node} - AssertionError: expected mod::func\n"
    assert parser(log) == {node: "FAILED"}


@pytest.mark.parametrize(
    "trailer",
    ["[ 8/17]", "[17/17]", "415.6us", "2.4ms", "1.2s", "2m 5s", "1h 3m"],
)
def test_non_default_console_output_styles(parser, trailer):
    node = "test_cases.py::test_count_style[1 + 1]"
    assert parser(f"{node} PASSED {trailer}\n") == {node: "PASSED"}


def test_status_words_inside_parameter_do_not_win(parser):
    node = "tests/test_calc.py::test_eval[PASSED [ 50%] then FAILED]"
    assert parser(f"{node} PASSED [100%]\n") == {node: "PASSED"}


def test_distinct_parameters_and_last_write_wins(parser):
    first = "tests/test_calc.py::test_eval[1 + 1]"
    second = "tests/test_calc.py::test_eval[1 + 2]"
    log = (
        f"{first} PASSED [ 50%]\n"
        f"{second} PASSED [100%]\n"
        f"ERROR {first} - RuntimeError: teardown failed [detail]\n"
    )
    assert parser(log) == {first: "ERROR", second: "PASSED"}


def test_long_unrelated_status_heavy_line_is_ignored(parser):
    log = (("noise PASSED FAILED SKIPPED ERROR " * 10_000) + "[100%]\n")
    assert parser(log) == {}


@pytest.mark.parametrize(
    "log",
    ["", "FAILED", "PASSED ", "SKIPPED [2]", "Some output PASSED [100%]"],
)
def test_empty_malformed_and_unrelated_lines(parser, log):
    assert parser(log) == {}


def test_runtime_grading_matches_full_spaced_node_id():
    node = "tests/test_calc.py::test_eval[1 + 1]"
    status_map = parse_runtime_pytest(f"{node} PASSED [100%]\n")

    result = grade([node], [], status_map)

    assert result["reward"] == 1.0
    assert result["resolved"] is True


def test_real_pytest_output_and_standalone_verifier(tmp_path: Path):
    """Exercise actual pytest 9 output and the verifier as a standalone script."""
    parameters = ["1 + 1", "left - right", "PASSED or FAILED", "unmatched [ bracket"]
    test_file = tmp_path / "test_calc.py"
    source = (
        "import pytest\n"
        f"@pytest.mark.parametrize('expr', {parameters!r})\n"
        "def test_eval(expr):\n"
        "    assert FIXED\n"
        "@pytest.mark.parametrize('expr', ['keep passing'])\n"
        "def test_keep(expr):\n"
        "    assert True\n"
        "@pytest.mark.parametrize('expr', ['optional dep'])\n"
        "def test_skip(expr):\n"
        "    pytest.skip('needs optional dep')\n"
    )
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="", COLUMNS="200")

    logs: list[str] = []
    for fixed, expected_exit in ((False, 1), (True, 0)):
        test_file.write_text(source.replace("FIXED", str(fixed)), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-v",
                "-rA",
                "--color=no",
                "-p",
                "no:cacheprovider",
                "test_calc.py",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == expected_exit, result.stdout + result.stderr
        logs.append(result.stdout)

    pre = parse_canonical_pytest(logs[0])
    post = parse_canonical_pytest(logs[1])
    assert parse_runtime_pytest(logs[0]) == pre
    assert parse_runtime_pytest(logs[1]) == post

    f2p = [
        name
        for name, status in pre.items()
        if status == "FAILED" and post.get(name) == "PASSED"
    ]
    p2p = [
        name
        for name, status in pre.items()
        if status == "PASSED" and post.get(name) == "PASSED"
    ]

    assert f2p == [f"test_calc.py::test_eval[{parameter}]" for parameter in parameters]
    assert p2p == ["test_calc.py::test_keep[keep passing]"]

    standalone = tmp_path / "verifier.py"
    standalone.write_text(
        Path(runtime_verifier.__file__).read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "out.log").write_text(logs[1], encoding="utf-8")
    (tmp_path / "f2p.json").write_text(json.dumps(f2p), encoding="utf-8")
    (tmp_path / "p2p.json").write_text(json.dumps(p2p), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(standalone),
            "--log",
            "out.log",
            "--f2p",
            "f2p.json",
            "--p2p",
            "p2p.json",
            "--runner",
            "pytest",
            "--exit-code",
            "0",
            "--out-dir",
            "rewards",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    details = json.loads((tmp_path / "rewards/reward-details.json").read_text(encoding="utf-8"))
    assert details["reward"] == 1.0
    assert details["resolved"] is True
    assert details["parse_status"] == "ok"
