import subprocess
from unittest.mock import Mock

import pytest

from repo2rlenv.execution.read_retry import retry_read
from repo2rlenv.github import _run_gh


def test_observation_retries_timeout_but_not_validation(monkeypatch):
    monkeypatch.setattr("repo2rlenv.execution.read_retry.time.sleep", lambda _: None)
    operation = Mock(side_effect=[TimeoutError(), "completed"])
    assert retry_read(operation) == "completed"
    assert operation.call_count == 2
    invalid = Mock(side_effect=ValueError("invalid response"))
    with pytest.raises(ValueError):
        retry_read(invalid)
    assert invalid.call_count == 1


def test_daytona_connection_timeout_retries_observation(monkeypatch):
    monkeypatch.setattr("repo2rlenv.execution.read_retry.time.sleep", lambda _: None)
    error = type("DaytonaConnectionTimeoutError", (Exception,), {})()
    operation = Mock(side_effect=[error, "completed"])
    assert retry_read(operation) == "completed"
    assert operation.call_count == 2


@pytest.mark.parametrize("status,attempts", [(503, 3), (429, 3), (401, 1), (404, 1)])
def test_read_retries_transient_http_status_only(monkeypatch, status, attempts):
    monkeypatch.setattr("repo2rlenv.execution.read_retry.time.sleep", lambda _: None)
    error = RuntimeError("Provider response")
    error.status_code = status
    operation = Mock(side_effect=error)
    with pytest.raises(RuntimeError):
        retry_read(operation)
    assert operation.call_count == attempts


@pytest.mark.parametrize(
    "args,retries",
    [
        (["api", "repos/a/b"], 3),
        (["api", "repos/a/b", "--method", "POST"], 1),
        (["api", "repos/a/b", "-XPOST"], 1),
        (["api", "repos/a/b", "--method=POST"], 1),
        (["api", "repos/a/b", "-fstate=closed"], 1),
        (["api", "repos/a/b", "--field=state=closed"], 1),
        (["api", "repos/a/b", "--input=payload.json"], 1),
        (["pr", "list", "--repo", "a/b"], 3),
    ],
)
def test_github_only_retries_read_timeouts(monkeypatch, args, retries):
    monkeypatch.setattr("repo2rlenv.github.shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr("repo2rlenv.github.time.sleep", lambda _: None)
    run = Mock(side_effect=subprocess.TimeoutExpired("gh", 60))
    monkeypatch.setattr("repo2rlenv.github.subprocess.run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        _run_gh(args)
    assert run.call_count == retries
