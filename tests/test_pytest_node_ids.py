"""Node identities must survive generation-time and standalone pytest parsing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repo2rlenv.log_parsers.pytest_parser import parse_pytest
from repo2rlenv.pipelines import _pr_runtime_verifier as verifier


@pytest.fixture(params=[parse_pytest, verifier.parse_pytest], ids=["canonical", "standalone"])
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
    else:
        log = f"{node} FAILED" + (" [ 50%]" if style == "progress" else "") + "\n"
    assert parser(log) == {node: "FAILED"}


@pytest.mark.parametrize("status", ["PASSED", "FAILED", "SKIPPED", "ERROR"])
def test_summary_and_verbose_statuses_with_spaces(parser, status):
    node = "tests/test_calc.py::test_eval[1 + 1]"
    assert parser(f"{status} {node}\n") == {node: status}
    assert parser(f"{node} {status} [100%]\n") == {node: status}


@pytest.mark.parametrize(
    "suffix", ["[ 8/17]", "[ 17 / 17 ]", "415.6us", "12.3ms", "1.234s", "2m 3s", "1h 2m"]
)
def test_count_and_duration_suffixes(parser, suffix):
    nodes = {
        "tests/test_calc.py::test_plain": "PASSED",
        "tests/test_calc.py::test_eval[1 + 1]": "FAILED",
        "tests/test_calc.py::test_eval[a PASSED (b)]": "SKIPPED",
        "tests/test_calc.py": "ERROR",
    }
    log = "\n".join(
        f"{node} {status}"
        + (" (optional dependency)" if status == "SKIPPED" else "")
        + f" {suffix}"
        for node, status in nodes.items()
    )
    assert parser(log) == nodes


def test_large_assertion_diff_does_not_stall_parsing():
    # Isolate the timeout so a backtracking regression cannot hang the suite.
    # Both parsers should skip this ~130 KB non-test line in milliseconds;
    # the previous regex takes tens of seconds even on a fast machine.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from repo2rlenv.log_parsers.pytest_parser import parse_pytest\n"
            "from repo2rlenv.pipelines._pr_runtime_verifier import parse_pytest as standalone\n"
            "noise = 'E assert ' + ', '.join(f'[{i}, [x]]' for i in range(10000))\n"
            "node = 'tests/test_calc.py::test_eval[1 + 1]'\n"
            "log = noise + '\\n' + node + ' PASSED [100%]\\n'\n"
            "for parser in (parse_pytest, standalone):\n"
            "    assert parser(log) == {node: 'PASSED'}\n",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_skipped_reason_and_count_prefix(parser):
    node = "tests/test_calc.py::test_eval[a PASSED (b)]"
    assert parser(f"{node} SKIPPED (requires optional dependency) [100%]\n") == {node: "SKIPPED"}
    assert parser("SKIPPED [2] tests/test_calc.py:10: optional dependency\n") == {
        "tests/test_calc.py:10:": "SKIPPED"
    }


def test_distinct_parameters_and_last_write_wins(parser):
    first = "tests/test_calc.py::test_eval[1 + 1]"
    second = "tests/test_calc.py::test_eval[1 + 2]"
    log = (
        f"{first} PASSED [ 50%]\n"
        f"{second} PASSED [100%]\n"
        f"ERROR {first} - RuntimeError: teardown failed [detail]\n"
    )
    assert parser(log) == {first: "ERROR", second: "PASSED"}


@pytest.mark.parametrize(
    "log",
    ["", "FAILED", "PASSED ", "SKIPPED [2]", "Some output PASSED [100%]"],
)
def test_empty_malformed_and_unrelated_lines(parser, log):
    assert parser(log) == {}


@pytest.mark.parametrize("report_flag", ["-ra", "-rA"])
@pytest.mark.parametrize("output_style", ["progress", "classic", "count", "times"])
@pytest.mark.parametrize("verbosity", ["-v", "-vv"])
def test_real_pytest_logs_preserve_oracle_and_standalone_grading(
    tmp_path: Path, report_flag, output_style, verbosity
):
    """Discover F2P/P2P from actual logs, then grade using the shipped verifier."""
    parameters = ["1 + 1", "1 + 2", "left - right", "PASSED or FAILED"]
    test_file = tmp_path / "test_calc.py"
    source = (
        "import pytest\n"
        f"@pytest.mark.parametrize('expr', {parameters!r})\n"
        "def test_eval(expr):\n"
        "    assert FIXED\n"
        "@pytest.mark.parametrize('expr', ['keep passing'])\n"
        "def test_keep(expr):\n"
        "    assert True\n"
        "@pytest.mark.parametrize('expr', ['not available'])\n"
        "def test_skip(expr):\n"
        "    pytest.skip('optional dependency')\n"
    )
    # Record pytest's own outcomes independently of its terminal formatting.
    (tmp_path / "conftest.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "outcomes = {}\n"
        "def pytest_runtest_logreport(report):\n"
        "    if report.when == 'call' or report.failed or report.skipped:\n"
        "        status = report.outcome.upper()\n"
        "        if report.failed and report.when != 'call':\n"
        "            status = 'ERROR'\n"
        "        outcomes[report.nodeid] = status\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    Path('outcomes.json').write_text(json.dumps(outcomes))\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="", COLUMNS="200")
    logs = []
    for fixed, expected_exit in [(False, 1), (True, 0)]:
        test_file.write_text(source.replace("FIXED", str(fixed)), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                verbosity,
                report_flag,
                "-o",
                f"console_output_style={output_style}",
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
        outcomes = json.loads((tmp_path / "outcomes.json").read_text())
        assert len(outcomes) == len(parameters) + 2
        for parser in (parse_pytest, verifier.parse_pytest):
            parsed = parser(result.stdout)
            assert {node: parsed.get(node) for node in outcomes} == outcomes
        logs.append(result.stdout)

    pre, post = map(parse_pytest, logs)
    f2p = [
        name for name, status in pre.items() if status == "FAILED" and post.get(name) == "PASSED"
    ]
    p2p = [
        name for name, status in pre.items() if status == "PASSED" and post.get(name) == "PASSED"
    ]
    assert f2p == [f"test_calc.py::test_eval[{parameter}]" for parameter in parameters]
    assert p2p == ["test_calc.py::test_keep[keep passing]"]
    for log in logs:
        assert verifier.parse_pytest(log) == parse_pytest(log)

    # Run the verifier in isolation, with no installed repo2rlenv imports.
    standalone = tmp_path / "verifier.py"
    standalone.write_text(Path(verifier.__file__).read_text(encoding="utf-8"), encoding="utf-8")
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
    details = json.loads((tmp_path / "rewards/reward-details.json").read_text())
    assert details["reward"] == 1.0
    assert details["resolved"] is True
    assert details["parse_status"] == "ok"
    assert details["f2p_passed"] == len(parameters)
    assert details["p2p_passed"] == 1
