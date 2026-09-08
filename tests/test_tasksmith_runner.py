"""Execute only the trusted runner; every subprocess is a fixture, never target code."""

from __future__ import annotations

import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo2rlenv.tasksmith.emit import POLICY_VERSION, RUNNER


@pytest.fixture
def runner(tmp_path, monkeypatch):
    def run(
        *,
        observation="success",
        pytest_mode="failure",
        origin="success",
        submission="raise RuntimeError('learner bug')\n",
    ):
        workspace = tmp_path / "workspace"
        tests = tmp_path / "tests"
        out = tmp_path / "logs/verifier"
        reports = tmp_path / "reports"
        package = workspace / "src/example/__init__.py"
        package.parent.mkdir(parents=True)
        package.write_text(submission)  # Data only; no import or execution.
        tests.mkdir()
        out.mkdir(parents=True)
        contract = {
            "source_paths": ["src/example"],
            "min_tests": 3,
            "requirements": [
                {"tests": ["test_first", "test_second", "test_third"]},
            ],
        }
        (tests / "contract.json").write_text(json.dumps(contract))
        (tests / "materialization.json").write_text(json.dumps({"package_name": "example"}))
        (tests / "source_origin_probe.py").write_text(
            "import example\nprint('never executed locally')\n"
        )
        # Previous output must not be mistaken for a new witness or reward.
        (out / "source-observation.json").write_text('{"value":"stale proof"}')
        (out / "source-observation-error.json").write_text('{"kind":"stale error"}')
        (out / "reward.txt").write_text("1\n")
        calls = []

        def fake_subprocess(command, **kwargs):
            if "find_spec" in command[-1]:
                calls.append("origin")
                if origin == "launch_error":
                    raise OSError("origin launcher unavailable")
                if origin == "failed":
                    return subprocess.CompletedProcess(command, 1, "", "origin error")
                return subprocess.CompletedProcess(command, 0, json.dumps(str(package)), "")
            if "import example" in command[-1]:
                calls.append("observation")
                assert kwargs["timeout"] == 60 and kwargs["errors"] == "replace"
                if observation == "launch_error":
                    raise FileNotFoundError("worker launcher missing")
                if observation == "timeout":
                    raise subprocess.TimeoutExpired(
                        command, 60, output=b"partial", stderr=b"timed out"
                    )
                if observation == "nonzero":
                    return subprocess.CompletedProcess(command, 1, "", "RuntimeError: learner bug")
                output = {
                    "invalid_json": "not JSON",
                    "nonfinite": "NaN",
                    "oversized": "x" * 1_000_001,
                }.get(observation, "[1, 2]")
                return subprocess.CompletedProcess(command, 0, output, "")
            assert "pytest" in command, f"Unexpected subprocess: {command}"
            calls.append("pytest")
            assert "-I" in command and kwargs["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
            if pytest_mode == "timeout":
                raise subprocess.TimeoutExpired(command, 240)
            report = Path(
                next(part.split("=", 1)[1] for part in command if part.startswith("--junitxml="))
            )
            suite = ET.Element("testsuite")
            names = ["test_first", "test_second", "test_third"]
            if pytest_mode == "missing":
                names.pop()
            for name in names:
                case = ET.SubElement(suite, "testcase", name=name)
                if pytest_mode in {"failure", "skipped", "error"}:
                    ET.SubElement(case, pytest_mode).text = "trusted simulated pytest report"
            if pytest_mode != "no_report":
                ET.ElementTree(suite).write(report)
            code = 2 if pytest_mode == "collection_error" else int(pytest_mode == "failure")
            return subprocess.CompletedProcess(command, code, "protected tests executed", "")

        monkeypatch.setattr(subprocess, "run", fake_subprocess)
        # Redirect trusted absolute locations into a temporary fixture. None of
        # the source/probe/test file contents is evaluated; run() is fully mocked.
        code = (
            RUNNER.replace("/logs/verifier", str(out))
            .replace("/tmp/tasksmith-pytest", str(reports))
            .replace("/workspace", str(workspace))
            .replace("/tests", str(tests))
        )
        exit_code = 0
        try:
            exec(compile(code, "<trusted-tasksmith-runner>", "exec"), {"__name__": "__main__"})
        except SystemExit as exc:
            exit_code = exc.code
        return SimpleNamespace(
            out=out,
            package=package,
            calls=calls,
            exit_code=exit_code,
            details=json.loads((out / "details.json").read_text()),
        )

    return run


@pytest.mark.parametrize(
    "observation, kind",
    [
        ("nonzero", "nonzero_exit"),
        ("timeout", "timeout"),
        ("invalid_json", "invalid_json"),
        ("nonfinite", "invalid_json"),
        ("oversized", "output_limit"),
    ],
)
def test_target_observation_failure_still_runs_complete_protected_tests(runner, observation, kind):
    result = runner(observation=observation)
    assert result.calls == ["origin", "observation", "pytest"]
    assert result.exit_code == 0 and result.details["valid"] is True
    assert result.details["outcome"] == "submission_failure" and result.details["n_tests"] == 3
    assert result.details["source_observation"] == "failed"
    assert (result.out / "reward.txt").read_text() == "0\n"
    assert not (result.out / "source-observation.json").exists()
    diagnostic = json.loads((result.out / "source-observation-error.json").read_text())
    assert diagnostic["kind"] == kind
    assert len(json.dumps(diagnostic)) < 18000


def test_syntax_error_submission_is_not_executed_locally_and_grades_zero(runner):
    result = runner(observation="nonzero", submission="def broken(:\n")
    assert result.details["valid"] and result.details["reward"] == 0
    assert result.calls[-1] == "pytest"


def test_probe_failure_does_not_invent_a_failed_assertion_or_successful_witness(runner):
    result = runner(observation="invalid_json", pytest_mode="success")
    assert result.details["valid"] and result.details["reward"] == 1
    assert result.details["failed"] == []
    assert not (result.out / "source-observation.json").exists()
    # Raw assertions pass, but this trial supplies no base/oracle source witness.


@pytest.mark.parametrize(
    "observation, origin",
    [("launch_error", "success"), ("success", "launch_error"), ("success", "failed")],
)
def test_launch_and_origin_failures_remain_infrastructure(runner, observation, origin):
    result = runner(observation=observation, origin=origin)
    assert result.exit_code == 2 and result.details["valid"] is False
    assert result.details["outcome"] == "incomplete"
    assert "pytest" not in result.calls
    assert not (result.out / "reward.txt").exists()
    assert not (result.out / "source-observation.json").exists()


@pytest.mark.parametrize(
    "pytest_mode", ["missing", "skipped", "error", "no_report", "collection_error", "timeout"]
)
def test_observation_failure_does_not_relax_protected_execution_gates(runner, pytest_mode):
    result = runner(observation="nonzero", pytest_mode=pytest_mode)
    assert result.calls == ["origin", "observation", "pytest"]
    assert result.exit_code == 2 and result.details["valid"] is False
    assert result.details["outcome"] == "incomplete"
    assert not (result.out / "reward.txt").exists()


def test_successful_observation_retains_real_value_and_origin_hash(runner):
    result = runner(observation="success", pytest_mode="success")
    value = json.loads((result.out / "source-observation.json").read_text())
    assert value == {
        "value": [1, 2],
        "package_origin": str(result.package),
        "origin_sha256": hashlib.sha256(result.package.read_bytes()).hexdigest(),
    }
    assert result.details["source_observation"] == "passed"
    assert not (result.out / "source-observation-error.json").exists()
    assert POLICY_VERSION == 2
