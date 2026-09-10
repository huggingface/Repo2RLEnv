from __future__ import annotations

import json
import subprocess

import pytest

from repo2rlenv.execution import job


def test_supervisor_refuses_local_execution(tmp_path, monkeypatch):
    monkeypatch.delenv("REPO2RLENV_REMOTE_WORKER", raising=False)
    with pytest.raises(RuntimeError, match="remote worker"):
        job.execute(tmp_path / "job", ["forbidden-command"], timeout_sec=1)
    assert not (tmp_path / "job").exists()


def test_timeout_terminates_process_group_and_its_owned_containers(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO2RLENV_REMOTE_WORKER", "1")
    calls = []

    class Process:
        pid = 12345

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("command", timeout)
            return -9

    def start(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(job.subprocess, "Popen", start)
    monkeypatch.setattr(job.os, "killpg", lambda *args: calls.append(args))
    monkeypatch.setattr(job, "_cleanup_containers", lambda token: {"passed": True, "removed": 1})
    with pytest.raises(subprocess.TimeoutExpired):
        job.execute(tmp_path, ["remote-test-fixture"], timeout_sec=1)
    receipt = json.loads((tmp_path / "job.json").read_text())
    assert receipt["state"] == "failed"
    assert receipt["cleanup"] == {"passed": True, "removed": 1}
    assert calls[0][1]["env"]["REPO2RLENV_JOB_ID"] == receipt["job_id"]
    assert calls[1] == (12345, job.signal.SIGKILL)


def test_cleanup_filters_by_exact_job_ownership(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "container1\ncontainer2\n", "")

    monkeypatch.setattr(job.subprocess, "run", run)
    assert job._cleanup_containers("owned-id") == {"passed": True, "removed": 2}
    assert calls == [
        ["docker", "ps", "-aq", "--filter", "label=repo2rlenv.job=owned-id"],
        ["docker", "rm", "-f", "container1", "container2"],
    ]
