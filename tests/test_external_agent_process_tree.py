"""stop_process_tree must end the adapter *and* its descendants on every OS.

`os.killpg` and `signal.SIGKILL` do not exist on Windows; the cleanup used to
raise AttributeError there (masking the real failure) and would have left the
adapter's children running.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from repo2rlenv.tasksmith.author import external_agent
from repo2rlenv.tasksmith.author.external_agent import (
    ProcessCleanupError,
    _stop_and_wait,
    stop_process_tree,
)

# The child starts a grandchild that touches a heartbeat file every 50 ms, then
# waits. If the tree is really gone, the heartbeat stops advancing.
_GRANDCHILD = (
    "import pathlib, sys, time\n"
    "p = pathlib.Path(sys.argv[1])\n"
    "while True:\n"
    "    p.write_text(str(time.time()))\n"
    "    time.sleep(0.05)\n"
)
_CHILD = (
    "import subprocess, sys, time\n"
    f"subprocess.Popen([sys.executable, '-c', {_GRANDCHILD!r}, sys.argv[1]])\n"
    "time.sleep(600)\n"
)


async def _start_tree(heartbeat: Path, child: str = _CHILD) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        str(heartbeat),
        start_new_session=True,  # what run_external_agent does; ignored on Windows
    )
    deadline = time.monotonic() + 20
    while not heartbeat.exists():
        assert time.monotonic() < deadline, "grandchild never started"
        await asyncio.sleep(0.05)
    return process


async def _heartbeat_stopped(heartbeat: Path) -> bool:
    """True once the file stops changing for a full second."""
    last, quiet_since = heartbeat.read_text(), time.monotonic()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        current = heartbeat.read_text()
        if current != last:
            last, quiet_since = current, time.monotonic()
        elif time.monotonic() - quiet_since >= 1.0:
            return True
    return False


@pytest.mark.asyncio
async def test_force_stop_ends_the_adapter_and_its_descendants(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    process = await _start_tree(heartbeat)
    try:
        stop_process_tree(process, force=True)
        await asyncio.wait_for(process.wait(), timeout=15)
        assert await _heartbeat_stopped(heartbeat), "a descendant outlived the adapter"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_polite_stop_then_force_stop_leaves_nothing_running(tmp_path: Path) -> None:
    """The order run_external_agent uses: request, wait, then force."""
    heartbeat = tmp_path / "heartbeat"
    process = await _start_tree(heartbeat)
    try:
        stop_process_tree(process, force=False)
        await asyncio.wait_for(process.wait(), timeout=15)
        # The polite request may leave a descendant behind on some platforms;
        # the force pass that follows in run_external_agent must clean it up.
        try:
            stop_process_tree(process, force=True)
        except ProcessLookupError:
            pass
        assert await _heartbeat_stopped(heartbeat), "a descendant outlived the cleanup"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_stopping_an_already_finished_adapter_does_not_crash(tmp_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await process.wait()
    try:
        stop_process_tree(process, force=True)
    except ProcessLookupError:
        pass  # the documented POSIX outcome; run_external_agent suppresses it


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handling")
async def test_cleanup_kills_descendant_that_ignores_term_after_parent_exits(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    stubborn = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + _GRANDCHILD
    process = await _start_tree(heartbeat, _CHILD.replace(repr(_GRANDCHILD), repr(stubborn)))
    try:
        await _stop_and_wait(process)
        assert await _heartbeat_stopped(heartbeat)
    finally:
        try:
            stop_process_tree(process, force=True)
        except ProcessLookupError:
            pass


def test_windows_nonzero_taskkill_is_not_success_while_parent_is_alive(monkeypatch):
    monkeypatch.setattr(external_agent.sys, "platform", "win32")
    monkeypatch.setattr(
        external_agent.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=5)
    )
    process = SimpleNamespace(pid=12345, returncode=None)
    with pytest.raises(ProcessCleanupError, match="taskkill failed"):
        stop_process_tree(process, force=True)


class _StuckProcess:
    pid = 12345
    returncode = None
    killed = False

    async def wait(self):
        await asyncio.Event().wait()

    async def communicate(self):
        await asyncio.Event().wait()

    def kill(self):
        self.killed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_taskkill_failures_and_final_wait_are_bounded(monkeypatch, timeout):
    process = _StuckProcess()
    monkeypatch.setattr(external_agent.sys, "platform", "win32")
    monkeypatch.setattr(external_agent, "_PROCESS_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(external_agent, "_PROCESS_KILL_SECONDS", 0.01)
    attempts = []

    def taskkill(command, **kwargs):
        attempts.append(command)
        assert kwargs["timeout"] == 15
        if timeout:
            raise subprocess.TimeoutExpired(command, 15)
        return SimpleNamespace(returncode=5)

    monkeypatch.setattr(external_agent.subprocess, "run", taskkill)
    with pytest.raises(ProcessCleanupError, match="Could not stop runtime"):
        await asyncio.wait_for(_stop_and_wait(process), timeout=2)
    assert len(attempts) == 2
    assert process.killed


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [RuntimeError("provider failed"), asyncio.CancelledError()])
async def test_cleanup_failure_preserves_original_error_and_removes_token(
    monkeypatch, tmp_path, original
):
    process = _StuckProcess()

    class FailedBridge:
        url, token = "http://127.0.0.1:1", "test-token"
        failure = original

        def __init__(self, **kwargs):
            self.failed = asyncio.Event()
            self.failed.set()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def record(self, *args, **kwargs):
            pass

        def stop(self, **kwargs):
            pass

    monkeypatch.setattr(external_agent, "AgentBridge", FailedBridge)
    monkeypatch.setattr(external_agent, "runtime_path", lambda _: tmp_path / "runtime.mjs")
    monkeypatch.setattr(
        external_agent.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(
        external_agent, "_stop_and_wait", AsyncMock(side_effect=ProcessCleanupError("still alive"))
    )
    with pytest.raises(type(original)) as caught:
        await external_agent.run_external_agent(
            engine="pi",
            model="anthropic/claude-sonnet-4-6",
            system="test",
            prompt="test",
            budget=None,
            tools=[],
            handlers={},
            trace=tmp_path / "trace.jsonl",
            max_turns=1,
        )
    assert caught.value is original
    assert any("cleanup also failed" in note for note in original.__notes__)
    assert not list(tmp_path.rglob("runner-config.json"))
