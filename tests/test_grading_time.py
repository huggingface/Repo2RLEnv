"""Grading time limits: measured at generation, enforced per run, a hang scores 0."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo2rlenv.pipelines import _grader_launcher as launcher
from repo2rlenv.pipelines import _pr_runtime_verifier as verifier
from repo2rlenv.pipelines._eval_script import build_single_file_eval_script
from repo2rlenv.pipelines._grading_time import GradingTime, measure
from repo2rlenv.pipelines.pr_runtime import build_eval_script, verifier_config
from repo2rlenv.pipelines.pr_runtime_validate import _build_stage_script

_BASE = "a" * 40


@pytest.mark.parametrize(
    ("run", "overhead", "run_timeout", "verifier_timeout", "over_cap"),
    [
        (5.0, 3.0, 120, 600, False),  # floors: per run and Harbor's default
        (95.0, 10.0, 210, 600, False),  # 2 x 95 -> 190 -> 210 (30 s steps)
        (400.0, 20.0, 810, 1080, False),  # 810 + 1.5 x 20 + 125 (parse) + 60 -> 1080
        (1500.0, 30.0, 3000, 3000, True),  # needs 3,240 s: dropped at generation
    ],
)
def test_limits_follow_the_slowest_measured_run(
    run, overhead, run_timeout, verifier_timeout, over_cap
):
    timing = GradingTime(run_seconds=run, overhead_seconds=overhead)
    assert timing.run_timeout == run_timeout
    assert timing.verifier_timeout == verifier_timeout
    assert timing.over_cap is over_cap
    assert timing.metadata()["run_timeout_sec"] == run_timeout


def test_measure_takes_the_maximum_of_each_part():
    timing = measure([(10.0, 14.0), (12.0, 13.0), (9.0, 20.0)], [1.5, 40.0])
    assert timing.run_seconds == 12.0 and timing.overhead_seconds == 11.0
    assert timing.copy_seconds == 40.0
    assert measure([]) is None


@pytest.mark.parametrize(("copy", "limit"), [(0.0, 120), (3.0, 120), (40.0, 210)])
def test_copy_limit_follows_the_measured_copy(copy, limit):
    timing = GradingTime(run_seconds=1, overhead_seconds=1, copy_seconds=copy)
    assert timing.copy_timeout == limit
    assert timing.metadata()["copy_timeout_sec"] == limit


def test_verifier_config_sets_the_measured_timeout():
    config = verifier_config(GradingTime(run_seconds=400, overhead_seconds=20))
    assert config["timeout_sec"] == 1080.0 and config["environment_mode"] == "separate"
    assert verifier_config(None)["timeout_sec"] == 600.0


@pytest.mark.parametrize(
    ("raw", "scaled"), [("", 100.0), ("2", 200.0), ("x", 100.0), ("-1", 100.0)]
)
def test_timeout_scale_env(monkeypatch, raw, scaled):
    monkeypatch.setenv("R2E_TIMEOUT_SCALE", raw)
    assert launcher.scaled_timeout(100) == scaled
    assert launcher.scaled_timeout(None) is None


def test_scripts_pass_the_limit_to_the_launcher():
    script = build_eval_script(
        _BASE,
        "",
        ["pytest t.py"],
        fail_to_pass=["t.py::test_a"],
        run_timeout=210,
    )
    assert script.count("--timeout 210") == 1
    assert '--timed-out "$R2E_TIMED_OUT"' in script
    single = build_single_file_eval_script("test_x.py", run_timeout=120)
    assert single.count("--timeout 120") == 1 and '--timed-out "$R2E_TIMED_OUT"' in single


def test_validation_records_test_seconds():
    script = _build_stage_script(
        _BASE, apply_patch=None, apply_test_patch=None, test_cmds=["pytest"]
    )
    assert "--seconds-out /opt/r2e-validate/seconds" in script
    assert "R2E_TEST_SECONDS=" in script


def _main(tmp_path: Path, log: str, *extra: str) -> dict:
    (tmp_path / "a.log").write_text(log)
    (tmp_path / "f2p.json").write_text(json.dumps(["t.py::test_a"]))
    (tmp_path / "p2p.json").write_text("[]")
    verifier.main(
        [
            "--log",
            str(tmp_path / "a.log"),
            "--runner",
            "pytest",
            "--f2p",
            str(tmp_path / "f2p.json"),
            "--p2p",
            str(tmp_path / "p2p.json"),
            "--out-dir",
            str(tmp_path),
            *extra,
        ]
    )
    details = json.loads((tmp_path / "reward-details.json").read_text())
    details["reward_txt"] = (tmp_path / "reward.txt").read_text().strip()
    return details


def test_timeout_is_a_graded_zero(tmp_path: Path):
    details = _main(tmp_path, "t.py::test_a PASSED\n", "--exit-code", "124", "--timed-out", "1")
    assert details["reward_txt"] == "0.000000"
    assert details["parse_status"] == "test_timeout"
    assert details["evaluation_status"] == "graded"


def test_graded_run_records_status_and_wall_time(tmp_path: Path):
    (tmp_path / "a.seconds").write_text("10.0\n")
    details = _main(
        tmp_path,
        "t.py::test_a PASSED\n",
        "--exit-code",
        "0",
        "--seconds",
        str(tmp_path / "a.seconds"),
    )
    assert details["reward_txt"] == "1.000000"
    assert details["evaluation_status"] == "graded" and details["run_seconds"] == 10.0
