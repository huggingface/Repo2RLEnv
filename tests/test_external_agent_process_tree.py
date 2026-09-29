"""stop_process_tree must end the adapter *and* its descendants on every OS.

`os.killpg` and `signal.SIGKILL` do not exist on Windows; the cleanup used to
raise AttributeError there (masking the real failure) and would have left the
adapter's children running.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from repo2rlenv.tasksmith.author.external_agent import stop_process_tree

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


async def _start_tree(heartbeat: Path) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CHILD,
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
