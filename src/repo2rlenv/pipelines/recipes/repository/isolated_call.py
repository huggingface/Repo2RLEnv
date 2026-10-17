"""Trusted-side JSON calls into one reserved Linux worker identity.

This module is copied into a dedicated, network-disabled verifier environment.
Assertions stay in the root-owned caller, which must never import submitted code.
The worker's JSON is a claimed value, never a test verdict. UID isolation is not a
replacement for the outer Harbor sandbox; the UID must be exclusive to this call.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class WorkerFailure(RuntimeError):
    """No trustworthy value was returned; the caller must fail closed."""


def _pids(uid: int) -> set[int]:
    result = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / "status").read_text()
        except FileNotFoundError:
            continue
        # Include real, effective, saved and filesystem identities.
        identities = next(line for line in status.splitlines() if line.startswith("Uid:"))
        if uid in map(int, identities.split()[1:]):
            result.add(int(entry.name))
    return result


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Required Linux process restriction failed")


def _restrict() -> None:
    import resource

    # Executed only in this standalone verifier's child, before untrusted imports.
    _prctl(38, 1)  # PR_SET_NO_NEW_PRIVS: setuid programs cannot recover privileges.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))


def _quiesce(uid: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5
    while True:
        # setsid/double-fork cannot escape the dedicated UID. The outer sandbox
        # must contain no unrelated process using this identity.
        for pid in _pids(uid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.poll()
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        if not _pids(uid):
            return
        if time.monotonic() >= deadline:
            raise WorkerFailure("Worker identity did not become quiescent")
        time.sleep(0.02)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def decode_value(raw: bytes, *, limit: int = 1024 * 1024):
    """Parse bounded data without pickle, eval or executable object hooks."""
    if len(raw) > limit:
        raise WorkerFailure("Worker output exceeded the byte limit")
    try:
        return json.loads(
            raw, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise WorkerFailure("Worker did not return one finite JSON value") from exc


def call(
    argv: list[str], request, *, timeout: int = 30, uid: int = 1001, output_limit: int = 1024 * 1024
):
    """Return untrusted data after termination of all worker descendants.

    Invoke only from a single-threaded, dedicated Linux verifier running as root.
    Keep all private assertions and references in root-only directories. The
    parent retains responsibility for schema checks and every behavioral assertion.
    """
    if sys.platform != "linux" or os.geteuid() != 0 or uid <= 0:
        raise WorkerFailure("Isolated calls require Linux root and a reserved unprivileged UID")
    import fcntl

    if not argv or not 1 <= timeout <= 300 or not 1 <= output_limit <= 1024 * 1024:
        raise ValueError("Invalid worker command or limits")
    lock_dir = Path("/run/repo2rlenv-isolated")
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    if lock_dir.is_symlink() or lock_dir.stat().st_uid != 0 or lock_dir.stat().st_mode & 0o077:
        raise WorkerFailure("Worker lock directory must be private and root-owned")
    descriptor = os.open(lock_dir / f"{uid}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkerFailure("Another call owns this worker UID") from exc
        return _call_locked(argv, request, timeout=timeout, uid=uid, output_limit=output_limit)


def _call_locked(argv, request, *, timeout, uid, output_limit):
    if _pids(uid):
        raise WorkerFailure("Worker UID is already in use; refusing to affect unrelated processes")
    encoded = json.dumps(request, allow_nan=False).encode()
    if len(encoded) > 1024 * 1024:
        raise ValueError("Worker input exceeded the byte limit")
    _prctl(36, 1)  # PR_SET_CHILD_SUBREAPER: adopt and reap detached descendants.
    with tempfile.TemporaryDirectory(prefix="r2e-isolated-") as name:
        directory = Path(name)  # root-owned mode 0700; child receives only open FDs.
        input_path = directory / "input.json"
        input_path.write_bytes(encoded)
        with (
            input_path.open("rb") as stdin,
            (directory / "stdout").open("w+b") as stdout,
            (directory / "stderr").open("w+b") as stderr,
        ):
            process = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                user=uid,
                group=uid,
                extra_groups=[],
                preexec_fn=_restrict,
                start_new_session=True,
                cwd="/workspace",
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/tmp",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "OMP_NUM_THREADS": "1",
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                },
            )
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise WorkerFailure("Worker timed out") from exc
            finally:
                _quiesce(uid, process)
            if code != 0:
                raise WorkerFailure(f"Worker exited with status {code}")
            stdout.seek(0)
            return decode_value(stdout.read(output_limit + 1), limit=output_limit)
