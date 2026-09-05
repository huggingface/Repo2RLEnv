from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from repo2rlenv.curation import cloud


def sandbox_with_process(*, exit_code=0, stdout="ok", stderr=""):
    process = SimpleNamespace(
        stdout=SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=stdout))),
        stderr=SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=stderr))),
        wait=SimpleNamespace(aio=AsyncMock(return_value=exit_code)),
    )
    sandbox = cloud.AuthorSandbox()
    sandbox.sandbox = SimpleNamespace(exec=SimpleNamespace(aio=AsyncMock(return_value=process)))
    return sandbox, process


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested,effective", [(None, 120), (280, 280), (900, 300), (0, 1), (-20, 1)]
)
async def test_shell_reports_actual_outer_timeout_and_elapsed_time(
    monkeypatch, requested, effective
):
    sandbox, process = sandbox_with_process()
    clock = iter([100.0, 102.3456])
    monkeypatch.setattr(cloud, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    # The command's own deadline never determines the provider timeout.
    command = "timeout 280 python smoke.py"
    options = {} if requested is None else {"timeout_sec": requested}
    result = json.loads(await sandbox.shell(command, **options))
    sandbox.sandbox.exec.aio.assert_awaited_once_with("bash", "-lc", command, timeout=effective)
    process.wait.aio.assert_awaited_once()
    assert result == {
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "effective_timeout_sec": effective,
        "elapsed_seconds": 2.346,
    }


@pytest.mark.asyncio
async def test_abnormal_exit_has_actionable_neutral_feedback_and_preserves_output(monkeypatch):
    sandbox, _ = sandbox_with_process(exit_code=-1, stdout="partial diagnostic", stderr="")
    clock = iter([0.0, 119.75])
    monkeypatch.setattr(cloud, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    result = json.loads(await sandbox.shell("timeout 280 python mutation_check.py"))
    assert result["exit_code"] == -1 and result["stdout"] == "partial diagnostic"
    assert result["effective_timeout_sec"] == 120 and result["elapsed_seconds"] == 119.75
    assert "cause is not reported" in result["note"]
    assert "timeout_sec explicitly (maximum 300)" in result["note"]
    assert "split" in result["note"]
    assert "inner shell timeout cannot extend" in result["note"]
    assert "timed out" not in result["note"] and "provider killed" not in result["note"]


@pytest.mark.asyncio
async def test_regular_nonzero_exit_keeps_output_bounds_without_timeout_diagnosis(monkeypatch):
    sandbox, _ = sandbox_with_process(exit_code=2, stdout="x" * 21000, stderr="e" * 5000)
    clock = iter([40.0, 40.25])
    monkeypatch.setattr(cloud, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    result = json.loads(await sandbox.shell("bad-command", timeout_sec=300))
    assert result["exit_code"] == 2 and "note" not in result
    assert result["stdout"] == "x" * 20000 and result["stderr"] == "e" * 4000
    assert result["elapsed_seconds"] == 0.25 and result["effective_timeout_sec"] == 300
