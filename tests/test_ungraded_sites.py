"""Which grading outcomes write a reward file.

An outcome the submission can cause is a graded 0 (reward file written). Grader
infrastructure it can't reach leaves the run ungraded with no reward file, so
Harbor records the trial as VERIFIER_FAILED and RL consumers drop the rollout.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from repo2rlenv.pipelines._eval_script import build_single_file_eval_script
from repo2rlenv.pipelines.pr_runtime import build_eval_script, finish_function

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_UNGRADED = {
    "no_system_python",
    "submission_rebuild_failed",
    "submission_restore_failed",
    "grader_launch_failed",
    "verifier_error",
}
_GRADED_ZERO = {
    "submission_rebuild_crashed",
    "test_patch_apply_failed",
    "test_config_reset_failed",
    "runner_config_changed",
    "test_cleanup_failed",
    "log_unparseable",
}


def _finish(logs, *args: str) -> None:
    script = finish_function().replace("/logs/verifier", str(logs))
    (logs / "reward.txt").write_text("stale\n")  # a crashed step may leave one
    quoted = " ".join(f"'{arg}'" for arg in args)
    subprocess.run(["bash", "-c", f"{script}\nr2e_finish {quoted}\necho unreachable"], check=True)


@needs_bash
def test_ungraded_finish_leaves_no_reward_file(tmp_path):
    _finish(tmp_path, "ungraded", "grader_launch_failed", "launch")
    assert not (tmp_path / "reward.txt").exists()
    details = json.loads((tmp_path / "reward-details.json").read_text())
    assert details["reward"] is None
    assert details["evaluation_status"] == "ungraded"
    assert details["parse_status"] == "grader_launch_failed"


@needs_bash
def test_graded_finish_writes_zero(tmp_path):
    _finish(tmp_path, "graded", "test_patch_apply_failed", "apply")
    assert (tmp_path / "reward.txt").read_text() == "0.000000\n"
    details = json.loads((tmp_path / "reward-details.json").read_text())
    assert details["reward"] == 0.0
    assert details["evaluation_status"] == "graded"


def _statuses(script: str) -> dict[str, str]:
    """parse_status -> graded/ungraded for every `r2e_finish` site."""
    return {
        status: kind for kind, status in re.findall(r"r2e_finish (graded|ungraded) (\w+)", script)
    }


def test_separate_runtime_script_classifies_every_site():
    script = build_eval_script(
        "abc123",
        "diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n-a\n+b\n",
        ["python -m pytest t.py"],
        fail_to_pass=["t.py::test_a"],
    )
    statuses = _statuses(script)
    for status in _UNGRADED:
        assert statuses.get(status) == "ungraded", status
    for status in _GRADED_ZERO:
        assert statuses.get(status) == "graded", status
    # Submission rejections from the rebuild (too_large, apply_failed, ...) are graded.
    assert 'r2e_finish graded "submission_$R2E_SUBMISSION"' in script


def test_single_file_script_classifies_every_site():
    statuses = _statuses(build_single_file_eval_script("test_task.py"))
    for status in (
        "no_system_python",
        "submission_check_failed",
        "grader_launch_failed",
        "verifier_error",
    ):
        assert statuses.get(status) == "ungraded", status
    assert statuses.get("test_cleanup_failed") == "graded"
    assert statuses.get("log_unparseable") == "graded"
