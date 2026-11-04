from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from repo2rlenv.tasksmith.author.bridge import AgentBridge
from repo2rlenv.tasksmith.author.inference import (
    MAX_OUTPUT_TOKENS,
    MODEL_TIMEOUT_SEC,
    anthropic_options,
    inference_settings,
)

_PROCESS_GRACE_SECONDS = 12
_PROCESS_KILL_SECONDS = 3


class ProcessCleanupError(RuntimeError):
    """The adapter's process tree could not be confirmed stopped."""


def runtime_path(engine: str) -> Path:
    if engine not in {"pi", "opencode"}:
        raise ValueError(f"Unknown external runtime: {engine}")
    from repo2rlenv.tasksmith.runtime import directory

    root = directory()
    if not (root / "node_modules").is_dir() or not shutil.which("node"):
        raise RuntimeError(
            f"Install Node >=22.19 and run `repo2rlenv tasksmith install-runtime` for the {engine} adapter"
        )
    return root / f"{engine}.mjs"


def stop_process_tree(process: asyncio.subprocess.Process, *, force: bool) -> None:
    """End the adapter and everything it spawned.

    POSIX: the adapter leads its own session (``start_new_session``), so its pid
    is a process-group id and one signal reaches every descendant. Windows has
    neither ``os.killpg`` nor ``signal.SIGKILL`` and ignores
    ``start_new_session``; ``taskkill /T`` walks the parent/child tree instead.
    ``force=False`` is the polite first request (SIGTERM on POSIX), ``force=True``
    the last resort. Windows has no graceful group signal, and the tree can only
    be found while the adapter is still alive (``terminate()`` alone would orphan
    its children and ``taskkill /T`` cannot reach them afterwards), so both
    requests end the whole tree at once there.
    Raises ``ProcessLookupError`` on POSIX if the group is already gone.
    """
    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        if result.returncode and process.returncode is None:
            raise ProcessCleanupError(
                f"taskkill failed for runtime PID {process.pid}: exit {result.returncode}"
            )
        return
    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)


async def _stop_and_wait(process: asyncio.subprocess.Process) -> None:
    """Bound both shutdown attempts; a failed tree kill is never a silent success."""
    error = None
    for force in (False, True):
        try:
            # taskkill can take 15 seconds. Keep the bridge/event loop responsive.
            await asyncio.to_thread(stop_process_tree, process, force=force)
            error = None
        except ProcessLookupError:
            error = None  # POSIX: the process group is already gone.
        except (OSError, subprocess.SubprocessError, ProcessCleanupError) as exc:
            error = exc
        try:
            await asyncio.wait_for(
                process.wait(), timeout=_PROCESS_KILL_SECONDS if force else _PROCESS_GRACE_SECONDS
            )
        except TimeoutError as exc:
            if error is None:
                error = exc
        # Always attempt the force pass, even if SIGTERM ended the parent:
        # a descendant in the same POSIX group may have ignored SIGTERM.
    if error is None:
        return
    # Last resort: reap the direct child where possible. This does not prove
    # descendants stopped, so the cleanup error must still be reported.
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_KILL_SECONDS)
    raise ProcessCleanupError(f"Could not stop runtime process tree {process.pid}") from error


async def run_external_agent(
    *, engine, model, system, prompt, budget, tools, handlers, trace, max_turns, max_cost=8
):
    adapter = runtime_path(engine)
    trace.parent.mkdir(parents=True, exist_ok=True)
    session_dir = trace.with_suffix("").with_name(trace.stem + "-runtime").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    config_path = session_dir / "runner-config.json"
    async with AgentBridge(
        model=model,
        budget=budget,
        tools=tools,
        handlers=handlers,
        trace=trace,
        max_turns=max_turns,
        max_cost=max_cost,
    ) as bridge:
        bridge.record(
            "input",
            model=model,
            system=system,
            prompt=prompt,
            runtime=engine,
            inference=inference_settings(model),
            timeout_sec=MODEL_TIMEOUT_SEC,
        )
        config = {
            "model": model.split("/", 1)[1],
            "system": system,
            "prompt": prompt,
            "bridge_url": bridge.url,
            "bridge_token": bridge.token,
            "tools": tools,
            "session_dir": str(session_dir),
            "max_turns": max_turns,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "inference_options": anthropic_options(model),
            "model_timeout_sec": MODEL_TIMEOUT_SEC,
        }
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        env = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env["HOME"] = str(session_dir / "home")
        Path(env["HOME"]).mkdir(exist_ok=True)
        process = None
        failed = None
        communication = None
        completed = False
        try:
            with (session_dir / "runtime-stderr.log").open("wb") as errors:
                process = await asyncio.create_subprocess_exec(
                    shutil.which("node"),
                    str(adapter),
                    str(config_path),
                    cwd=session_dir,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=errors,
                    start_new_session=True,
                )
                communication = asyncio.create_task(process.communicate())
                failed = asyncio.create_task(bridge.failed.wait())
                done, _ = await asyncio.wait(
                    [communication, failed], timeout=3600, return_when=asyncio.FIRST_COMPLETED
                )
                if bridge.failure:
                    raise bridge.failure
                if communication not in done:
                    raise TimeoutError(f"{engine} author runtime exceeded one hour")
                stdout, _ = communication.result()
                (session_dir / "runtime-result.json").write_bytes(stdout)
                if process.returncode:
                    raise RuntimeError(
                        f"{engine} runtime exited {process.returncode}: "
                        + stdout.decode(errors="replace")[-2000:]
                    )
                result = json.loads(stdout)
                if result.get("error"):
                    raise RuntimeError(result["error"])
                completed = True
        finally:
            original_error = sys.exception()
            cleanup_error = None
            bridge.stop(cancel_models=not completed)
            if process is not None and process.returncode is None:
                try:
                    await _stop_and_wait(process)
                except (OSError, subprocess.SubprocessError, ProcessCleanupError) as exc:
                    cleanup_error = exc
            for task in (communication, failed):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            config_path.unlink(missing_ok=True)
            if cleanup_error is not None:
                if original_error is not None:
                    original_error.add_note(f"Runtime cleanup also failed: {cleanup_error}")
                else:
                    raise cleanup_error
    # __aexit__ drains successful provider streams, including settlement or any
    # error that arrives after the runtime has already produced its final JSON.
    if bridge.failure:
        raise bridge.failure
    return {"messages": result["messages"], "turns": bridge.turns, "cost": bridge.cost}
