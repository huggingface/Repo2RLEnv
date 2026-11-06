"""Run a task's test command as an unprivileged user, then stop everything it started.

Standalone and stdlib-only. Root runs it with an image interpreter and
``-I -S`` (no ``.pth`` files, no ``sitecustomize``), both in the separate
verifier container and during generation-time validation, so F2P/P2P are
measured the way they are graded.

    python3 -I -S _grader_launcher.py --log LOG --cwd DIR --path CHILD_PATH \
        [--writable DIR ...] [--fresh-copy VAR=DIR ...] [--timeout SEC] \
        [--purge [--purge-deadline SEC]] [--seconds-out FILE] -- COMMAND

It prints one word and exits 0: the command's exit code; ``timeout`` when the
command ran past ``--timeout`` (scaled by ``R2E_TIMEOUT_SCALE`` when that is
set); or ``cleanup_failed`` when the test user's processes, files or IPC
objects could not all be removed afterwards (code under test can cause that:
a fork bomb, a tree too deep to walk), which the caller grades 0; or
``unsafe_image``, before anything runs, when a path root later imports or
executes from is writable by the test user (an image problem: ungraded, and
validation drops the task).
``--seconds-out`` records the command's wall time. A failure before the
command starts exits 1 with nothing on stdout; the caller leaves the run
ungraded.

Code under test runs as a fixed UID with no supplementary groups and
``no_new_privs``. It can write the directories passed with ``--writable``
(the repository, and toolchain homes that must stay writable) and a fresh
HOME, nothing the root grader uses afterwards. Its output goes to a
root-owned file, in a directory only root can read, opened before the fork.
Every process with that UID is killed and its SysV IPC objects and POSIX
message queues removed before this script returns, so nothing can rewrite the
log. Files it left are deleted only with ``--purge``: the separate verifier
runs the tests once in a container that is then discarded, so deleting them
there would only give the submission a way to stall grading (a few million
files take minutes to remove). Callers that run tests again in the same
container (generation-time validation) pass ``--purge``; it runs under
``--purge-deadline`` and missing the deadline is a ``cleanup_failed``.
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

NAME = "r2e-grader"
UID = 20001
_SHARED_TMP = ("/tmp", "/var/tmp", "/dev/shm")
# Pseudo filesystems: nothing the test user writes persists there. POSIX
# message queues are removed separately (`_remove_ipc`).
_PRUNE = ("/proc", "/sys", "/dev/pts", "/dev/mqueue")
# Root-only grader directories: the mutant check keeps each run's tree there,
# out of the test user's reach. Never walked, so a run that fills its tree
# can't stall the next run's purge.
_GRADER_DIRS = ("/opt/r2e", "/opt/r2e-validate", "/.r2e-agent-tree-*")
_MQUEUE = "/dev/mqueue"
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Toolchain caches the test command must be able to write. They are redirected
# into the fresh HOME so a root-owned image path doesn't break the build.
_CACHE_VARS = {
    "GOCACHE": ".cache/go-build",
    "XDG_CACHE_HOME": ".cache",
    "npm_config_cache": ".npm",
    "YARN_CACHE_FOLDER": ".cache/yarn",
}


def _ensure_account() -> None:
    """Give UID a passwd/group entry; some tools refuse an unnamed user."""
    for path, line in (
        ("/etc/passwd", f"{NAME}:x:{UID}:{UID}::/nonexistent:/bin/sh\n"),
        ("/etc/group", f"{NAME}:x:{UID}:\n"),
    ):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                taken = any(
                    len(parts) > 2 and parts[2] == str(UID)
                    for parts in (entry.split(":") for entry in handle)
                )
            if not taken:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            # A read-only /etc still lets us switch to a numeric UID.
            pass


def _pids(uid: int) -> set[int]:
    found = set()
    for entry in os.listdir("/proc"):
        if not entry.isdecimal():
            continue
        try:
            with open(f"/proc/{entry}/status", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        # A zombie has already exited; it cannot run or write anything.
        if any(line.startswith("State:") and line.split()[1:2] == ["Z"] for line in lines):
            continue
        for line in lines:
            # Real, effective, saved and filesystem UIDs.
            if line.startswith("Uid:") and uid in map(int, line.split()[1:]):
                found.add(int(entry))
    return found


def _reap() -> None:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def _quiesce(uid: int) -> None:
    """Kill every process with `uid` until none is left (setsid can't escape)."""
    deadline = time.monotonic() + 15
    while True:
        for pid in _pids(uid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _reap()
        if not _pids(uid):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("test processes did not exit")
        time.sleep(0.05)


def _owned_paths(uid: int, keep: list[str], roots: list[str] | None = None) -> list[str]:
    """The topmost paths `uid` owns outside `keep`, on every filesystem it could write.

    `find -xdev` on / plus each shared temp dir (often a separate tmpfs),
    stopping at the first entry the user owns: its contents go with it, so a
    tree nested deeper than PATH_MAX is removed from a short path. The image's
    own find runs with a fixed PATH; the test user's files are data here.
    """
    if roots is None:
        roots = ["/", *(top for top in _SHARED_TMP if os.path.isdir(top))]
    prune = [*_PRUNE, *_GRADER_DIRS, *(os.path.abspath(path) for path in keep)]
    found: list[str] = []
    for root in roots:
        expression: list[str] = []
        for path in prune:
            expression += ["-path", path, "-o"]
        command = ["find", root, "-xdev", "(", *expression[:-1], ")", "-prune", "-o"]
        command += ["-user", str(uid), "-prune", "-print0"]
        result = subprocess.run(
            command, env={"PATH": _SAFE_PATH}, capture_output=True, timeout=300, check=False
        )
        # 1 = some entries vanished or were unreadable mid-walk; anything else is broken.
        if result.returncode not in (0, 1):
            raise RuntimeError(f"find failed on {root}: {result.stderr[-300:]!r}")
        found += [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]
    return sorted(set(found))


def _purge(uid: int, keep: list[str], roots: list[str] | None = None) -> None:
    """Delete what the test user left anywhere but `keep`, so two runs can't talk.

    `shutil.rmtree` walks with directory file descriptors, so depth is no
    obstacle; anything still there afterwards is an error.
    """
    for path in _owned_paths(uid, keep, roots):
        info = os.lstat(path)
        if info.st_uid != uid:
            continue
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    left = _owned_paths(uid, keep, roots)
    if left:
        raise RuntimeError(f"could not remove {len(left)} paths of the test user, e.g. {left[0]}")


# SysV IPC: /proc/sysvipc lists each object's id, key, owner and creator.
_IPC = {"shm": "shmid", "msg": "msqid", "sem": "semid"}
_IPC_RMID = 0


def _ipc_objects(uid: int, tables: str = "/proc/sysvipc") -> list[tuple[str, int]]:
    """(kind, id) of every SysV IPC object `uid` owns or created."""
    objects = []
    for kind in _IPC:  # one table per kind
        try:
            with open(os.path.join(tables, kind), encoding="utf-8") as handle:
                header = handle.readline().split()
                rows = [line.split() for line in handle if line.strip()]
        except OSError:
            continue
        column = {name: index for index, name in enumerate(header)}
        for row in rows:
            owners = {int(row[column[name]]) for name in ("uid", "cuid") if name in column}
            if uid in owners:
                objects.append((kind, int(row[column[_IPC[kind]]])))
    return objects


def _remove_ipc(uid: int) -> None:
    """Remove the test user's SysV shared memory, queues and semaphores and its
    POSIX message queues: state that outlives every process and every file.

    Root in a container usually lacks CAP_SYS_ADMIN, which removing another
    user's SysV objects needs, so a child that becomes the test user (their
    owner) removes them.
    """
    queues = []
    if os.path.isdir(_MQUEUE):
        queues = [
            e.path for e in os.scandir(_MQUEUE) if e.stat(follow_symlinks=False).st_uid == uid
        ]
    objects = _ipc_objects(uid)
    if not objects and not queues:
        return
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the forked child
        status = 1
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            libc = ctypes.CDLL(None, use_errno=True)
            removers = {
                "shm": lambda ident: libc.shmctl(ident, _IPC_RMID, None),
                "msg": lambda ident: libc.msgctl(ident, _IPC_RMID, None),
                "sem": lambda ident: libc.semctl(ident, 0, _IPC_RMID),
            }
            for kind, ident in objects:
                removers[kind](ident)
            for path in queues:
                os.unlink(path)
            status = 0
        finally:
            os._exit(status)
    os.waitpid(pid, 0)
    if _ipc_objects(uid) or any(os.path.lexists(path) for path in queues):
        raise RuntimeError("test user IPC objects remain")


PURGE_DEADLINE_SEC = 120.0


def _purge_within(uid: int, keep: list[str], deadline: float) -> None:
    """`_purge` in a child that is killed after `deadline` seconds."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the forked child
        status = 1
        try:
            _purge(uid, keep)
            status = 0
        finally:
            os._exit(status)
    end = time.monotonic() + deadline
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.monotonic() >= end:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise RuntimeError(f"deleting the test user's files took over {deadline:.0f} s")
        time.sleep(0.05)
    if os.waitstatus_to_exitcode(status) != 0:
        raise RuntimeError("could not delete every file the test user left")


def _cleanup(uid: int, keep: list[str], purge_deadline: float | None) -> None:
    """Stop the test user's processes and remove its IPC objects; with a
    deadline, delete its files outside `keep` too (see the module docstring)."""
    _quiesce(uid)
    _remove_ipc(uid)
    if purge_deadline is not None:
        _purge_within(uid, keep, purge_deadline)


def _chown_tree(root: str, uid: int) -> None:
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"writable path is not a directory: {root}")
    os.lchown(root, uid, uid)
    for directory, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            os.lchown(os.path.join(directory, name), uid, uid)


def _read_only(root: str) -> None:
    """Strip group/other write from a toolchain tree the test user only reads.

    Official toolchain images leave some world-writable (rust's CARGO_HOME and
    RUSTUP_HOME are 0777); the test user must not swap files there between runs.
    """
    for directory, dirnames, filenames in os.walk(root):
        for name in [directory, *(os.path.join(directory, n) for n in dirnames + filenames)]:
            info = os.lstat(name)
            if not stat.S_ISLNK(info.st_mode) and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                os.chmod(name, stat.S_IMODE(info.st_mode) & ~(stat.S_IWGRP | stat.S_IWOTH))


def _allow_traversal(path: str) -> None:
    """Let the test user reach `path` (e.g. /root/.cargo) without listing /root."""
    parent = os.path.dirname(os.path.abspath(path))
    while parent and parent != "/":
        mode = os.lstat(parent).st_mode
        if not mode & stat.S_IXOTH:
            os.chmod(parent, stat.S_IMODE(mode) | stat.S_IXOTH)
        parent = os.path.dirname(parent)


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), f"prctl({option}) failed")


def _child_setup() -> None:
    # setuid programs in the image (su, sudo) must not hand root back.
    _prctl(38, 1)  # PR_SET_NO_NEW_PRIVS


# The test output kept for grading and downloaded with the verifier's logs. Code
# under test chooses how much it prints; past this the log is cut, with a
# marker, so a multi-gigabyte log can't make Harbor's download fail. The
# verifier parses what is kept (well under its own read limit).
LOG_CAP = 16 * 1024 * 1024
_LOG_MARKER = b"\n[R2E: test output truncated at %d bytes]\n"


def _cap_log(descriptor: int, cap: int = LOG_CAP) -> None:
    """Cut the log behind `descriptor` (opened by root before the fork) to `cap`."""
    if os.fstat(descriptor).st_size <= cap:
        return
    os.ftruncate(descriptor, cap)
    os.lseek(descriptor, 0, os.SEEK_END)
    os.write(descriptor, _LOG_MARKER % cap)


def _open_log(path: str) -> int:
    directory = os.path.dirname(os.path.abspath(path))
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        # Not even readable: the test user must not see the log, or its name.
        raise ValueError("log directory must be a root-only directory (0700)")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def child_environment(path: str, home: str) -> dict[str, str]:
    """The image's environment, with a fresh HOME and the given PATH."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("R2E_")}
    env.update(
        {
            "PATH": path,
            "HOME": home,
            "USER": NAME,
            "LOGNAME": NAME,
            # A go.mod `toolchain` line must not make go fetch and run another Go,
            # and go.work, a go env file or a module proxy must not redirect it.
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOENV": "off",
            "GOPROXY": "off",
            # Bytecode the test run writes next to the source could be served
            # to a later run of the same tree; nothing is cached.
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    for key, relative in _CACHE_VARS.items():
        env[key] = os.path.join(home, relative)
    return env


def scaled_timeout(timeout: float | None) -> float | None:
    """`timeout` times R2E_TIMEOUT_SCALE (a positive number), for slower graders."""
    if timeout is None:
        return None
    try:
        scale = float(os.environ.get("R2E_TIMEOUT_SCALE", "") or 1.0)
    except ValueError:
        scale = 1.0
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0
    return timeout * scale


def _fresh_copy(spec: str, home: str, env: dict[str, str]) -> None:
    """`VAR=DIR`: give the test user its own copy of DIR, at a new path, as VAR.

    A toolchain home the command must write (cargo's) is copied per run, so
    the image's copy stays root-owned and nothing one run writes there
    reaches the next.
    """
    name, sep, source = spec.partition("=")
    if not sep or not name.isidentifier() or not source:
        raise ValueError(f"bad --fresh-copy: {spec!r}")
    if not os.path.isdir(source):
        return
    destination = os.path.join(home, f".r2e-{name.lower()}")
    subprocess.run(
        ["cp", "-a", source, destination], env={"PATH": _SAFE_PATH}, check=True, timeout=600
    )
    _chown_tree(destination, UID)
    env[name] = destination
    # Its binaries (cargo's rustup proxies) come from the copy, ahead of the image's.
    if os.path.isdir(os.path.join(destination, "bin")):
        env["PATH"] = os.path.join(destination, "bin") + ":" + env["PATH"]


_SITE_DIRS = ("site-packages", "dist-packages")


class UnsafeImage(Exception):
    """A path root later imports or executes from is writable by the test user."""


def _writable_by(info: os.stat_result, uid: int) -> bool:
    return (
        info.st_uid == uid
        or bool(info.st_mode & stat.S_IWOTH)
        or (info.st_gid == uid and bool(info.st_mode & stat.S_IWGRP))
    )


def _unsafe_entry(path: str, uid: int) -> bool:
    """Whether the test user could change `path` (or, for a link, its target)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if _writable_by(info, uid) and not stat.S_ISLNK(info.st_mode):
        return True
    if stat.S_ISLNK(info.st_mode):
        try:
            return _writable_by(os.stat(path), uid)
        except OSError:
            return False
    return False


def _git_exec_path() -> str | None:
    git = shutil.which("git", path=_SAFE_PATH)
    if git is None:
        return None
    try:
        found = (
            subprocess.run(
                [git, "--exec-path"],
                env={"PATH": _SAFE_PATH},
                capture_output=True,
                timeout=30,
                check=False,
            )
            .stdout.decode(errors="replace")
            .strip()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found if found.startswith("/") else None


def unsafe_image_paths(
    uid: int = UID,
    import_dirs: list[str] | None = None,
    exec_dirs: list[str] | None = None,
    limit: int = 20,
) -> list[str]:
    """Paths root later imports or executes from that the test user can write.

    Nothing the test user leaves behind is deleted after the run (the verifier
    container is discarded), so root's own later steps must not read anything
    that user could have changed: every directory and file on this
    interpreter's import path (`-I -S`: the standard library only), the fixed
    PATH root uses and git's exec path, and every ancestor of those (a
    writable ancestor without the sticky bit can swap a whole directory). An
    image that fails this can't be graded safely, whatever the submission.
    """
    if import_dirs is None:
        import_dirs = [entry for entry in sys.path if entry and os.path.isabs(entry)]
    if exec_dirs is None:
        exec_dirs = _SAFE_PATH.split(":")
        git = _git_exec_path()
        if git:
            exec_dirs.append(git)
    found: list[str] = []

    def flag(path: str) -> bool:
        found.append(path)
        return len(found) >= limit

    ancestors: set[str] = set()
    for root in [*import_dirs, *exec_dirs]:
        parent = os.path.dirname(os.path.abspath(root))
        while parent not in ancestors:
            ancestors.add(parent)
            if parent == os.path.dirname(parent):
                break
            parent = os.path.dirname(parent)
    for directory in sorted(ancestors):
        try:
            info = os.lstat(directory)
        except OSError:
            continue
        if _writable_by(info, uid) and not info.st_mode & stat.S_ISVTX and flag(directory):
            return found
    for root in import_dirs:  # the whole tree: a planted .pyc in any __pycache__ would run
        if _unsafe_entry(root, uid) and flag(root):
            return found
        if not os.path.isdir(root):
            continue
        for top, dirnames, filenames in os.walk(root):
            # `-S` never imports from site-packages, however big it is.
            dirnames[:] = [name for name in dirnames if name not in _SITE_DIRS]
            for name in dirnames + filenames:
                path = os.path.join(top, name)
                if _unsafe_entry(path, uid) and flag(path):
                    return found
    for root in exec_dirs:  # the directory and what is directly in it
        if not os.path.isdir(root):
            continue
        if _unsafe_entry(root, uid) and flag(root):
            return found
        with os.scandir(root) as listing:
            for entry in listing:
                if _unsafe_entry(entry.path, uid) and flag(entry.path):
                    return found
    return found


class CleanupFailed(Exception):
    """The command ran, but what it started or left could not all be removed."""


class PreCleanupFailed(Exception):
    """What an earlier command left could not be removed before this one started."""


def run(
    command: str,
    *,
    log: str,
    cwd: str,
    path: str,
    writable: list[str],
    readable: list[str] = (),
    fresh: list[str] = (),
    timeout: float | None,
    purge_deadline: float | None = None,
) -> tuple[int, bool, float]:
    """Run `command`; return (exit code, timed out, wall seconds).

    Raises CleanupFailed when the command ran but the cleanup after it failed;
    any other exception means the command never started.
    """
    if os.geteuid() != 0:
        raise RuntimeError("the grader launcher must start as root")
    unsafe = unsafe_image_paths(UID)
    if unsafe:
        raise UnsafeImage(", ".join(unsafe))
    _ensure_account()
    try:
        _cleanup(UID, writable, purge_deadline)
    except Exception as exc:
        raise PreCleanupFailed(f"{type(exc).__name__}: {exc}") from exc
    for directory in readable:
        # Read-only access, e.g. Go's module cache under root's 0700 home.
        if os.path.isdir(directory):
            _read_only(directory)
            _allow_traversal(os.path.join(directory, "."))
    for directory in writable:
        if os.path.lexists(directory):
            _chown_tree(directory, UID)
            _allow_traversal(directory)
    home = tempfile.mkdtemp(prefix="r2e-home-")
    os.chown(home, UID, UID)
    _allow_traversal(home)
    env = child_environment(path, home)
    for spec in fresh:
        _fresh_copy(spec, home, env)
    descriptor = _open_log(log)
    # Detached descendants are reparented here, not to the container's init,
    # which may never reap them.
    _prctl(36, 1)  # PR_SET_CHILD_SUBREAPER
    timed_out = False
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            ["/bin/bash", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            user=UID,
            group=UID,
            extra_groups=[],
            preexec_fn=_child_setup,
            start_new_session=True,
            close_fds=True,
        )
        try:
            code = process.wait(timeout=scaled_timeout(timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            code = process.wait()
    finally:
        seconds = time.monotonic() - started
        try:
            cleanup_error = None
            try:
                _cleanup(UID, writable, purge_deadline)
            except Exception as exc:
                cleanup_error = exc
            # Whatever happened, the kept log is bounded: Harbor downloads it.
            _cap_log(descriptor)
        finally:
            os.close(descriptor)
        if cleanup_error is not None:
            raise CleanupFailed(
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            ) from cleanup_error
    return (code if code >= 0 else 128 - code), timed_out, seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True)
    parser.add_argument("--cwd", default="/workspace")
    parser.add_argument("--path", required=True, help="PATH for the test command")
    parser.add_argument("--writable", action="append", default=[])
    parser.add_argument("--readable", action="append", default=[])
    parser.add_argument("--fresh-copy", action="append", default=[], dest="fresh")
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--seconds-out", default="")
    parser.add_argument("--purge", action="store_true", help="delete the test user's files")
    parser.add_argument("--purge-deadline", type=float, default=PURGE_DEADLINE_SEC)
    parser.add_argument("command")
    args = parser.parse_args(argv)
    try:
        code, timed_out, seconds = run(
            args.command,
            log=args.log,
            cwd=args.cwd,
            path=args.path,
            writable=args.writable,
            readable=args.readable,
            fresh=args.fresh,
            timeout=args.timeout,
            purge_deadline=args.purge_deadline if args.purge else None,
        )
        if args.seconds_out:
            with open(args.seconds_out, "w", encoding="utf-8") as handle:
                handle.write(f"{seconds:.3f}\n")
    except UnsafeImage as exc:  # before any test code: the image is the problem
        sys.stderr.write(f"R2E grader launcher: writable by the test user: {exc}\n")
        sys.stdout.write("unsafe_image\n")
        return 0
    except PreCleanupFailed as exc:  # the caller knows whether a run came before
        sys.stderr.write(f"R2E grader launcher: cleanup before the tests failed: {exc}\n")
        sys.stdout.write("precleanup_failed\n")
        return 0
    except CleanupFailed as exc:  # the tests ran: graded 0 by the caller
        sys.stderr.write(f"R2E grader launcher: cleanup after the tests failed: {exc}\n")
        sys.stdout.write("cleanup_failed\n")
        return 0
    except Exception as exc:  # the tests never started: ungraded
        sys.stderr.write(f"R2E grader launcher failed: {type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write("timeout\n" if timed_out else f"{code}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
