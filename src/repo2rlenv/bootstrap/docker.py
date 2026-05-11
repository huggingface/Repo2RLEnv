"""Thin wrappers around the `docker` CLI for the bootstrap agent.

We use the CLI directly rather than `docker-py` to keep deps light and to
match how Harbor's own executors invoke docker. All commands run via
subprocess; output is captured and returned to the caller.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def truncated(self, max_chars: int = 4000) -> str:
        """Combined stdout+stderr, truncated for putting in an LLM prompt."""
        combined = self.stdout
        if self.stderr.strip():
            combined += "\n--- stderr ---\n" + self.stderr
        if len(combined) > max_chars:
            tail = combined[-max_chars:]
            return f"... [{len(combined) - max_chars} chars elided] ...\n{tail}"
        return combined


class DockerError(RuntimeError):
    pass


def _run(args: list[str], *, timeout: int = 600, input_text: str | None = None) -> ExecResult:
    """Run a subprocess and return ExecResult. Never raises on non-zero exit."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ExecResult(
            exit_code=124,
            stdout=(exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, (bytes, bytearray)) else (exc.stdout or ""),
            stderr=f"[timeout after {timeout}s]",
            duration_sec=time.monotonic() - start,
        )
    return ExecResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_sec=time.monotonic() - start,
    )


def is_docker_available() -> bool:
    """Return True if `docker version` succeeds."""
    return _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=5).ok


def pull_image(image: str, *, platform: str | None = None, timeout: int = 600) -> ExecResult:
    args = ["docker", "pull"]
    if platform:
        args.extend(["--platform", platform])
    args.append(image)
    return _run(args, timeout=timeout)


# Matches the last "<layer>: <Status> [...]  12.3MB/45.7MB" line in a chunk
_DOCKER_PROGRESS_RE = re.compile(
    r"(?P<status>Pulling|Downloading|Extracting|Verifying|Waiting|Pull complete|Already exists)"
    r"[^\r\n]*",
)


def pull_image_streaming(
    image: str,
    *,
    platform: str | None = None,
    timeout: int = 600,
    on_progress: Callable[[str], None] | None = None,
) -> ExecResult:
    """Pull an image, streaming the most recent progress line to a callback.

    Docker uses carriage returns to overwrite the same line in a TTY; we read
    chunks and split on '\\r' and '\\n' to grab the most-recent meaningful line.
    Falls back to plain pull_image() if no callback is supplied.
    """
    if on_progress is None:
        return pull_image(image, platform=platform, timeout=timeout)

    args = ["docker", "pull"]
    if platform:
        args.extend(["--platform", platform])
    args.append(image)

    start = time.monotonic()
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    out_buf: list[str] = []
    last_line = ""

    def _pump() -> None:
        nonlocal last_line
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            out_buf.append(chunk)
            # Split on both \r (overwrites) and \n (new lines)
            parts = re.split(r"[\r\n]+", chunk)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                m = _DOCKER_PROGRESS_RE.search(part)
                if m or part != last_line:
                    last_line = part
                    try:
                        on_progress(part[:120])
                    except Exception:
                        pass

    pump_thread = threading.Thread(target=_pump, daemon=True)
    pump_thread.start()
    try:
        exit_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        pump_thread.join(timeout=2)
        return ExecResult(
            exit_code=124,
            stdout="".join(out_buf),
            stderr=f"[timeout after {timeout}s]",
            duration_sec=time.monotonic() - start,
        )
    pump_thread.join(timeout=2)
    full = "".join(out_buf)
    return ExecResult(
        exit_code=exit_code,
        stdout=full if exit_code == 0 else "",
        stderr=full if exit_code != 0 else "",
        duration_sec=time.monotonic() - start,
    )


class DockerSandbox:
    """A long-lived Docker container the bootstrap agent runs commands in.

    Lifecycle:
        sb = DockerSandbox.start(base_image, repo_dir, platform="linux/amd64")
        sb.exec("pip install -e .")
        sb.exec("pytest --collect-only")
        digest = sb.commit("r2e/bootstrap/django:abc123")
        sb.cleanup()

    Use as a context manager to guarantee cleanup:
        with DockerSandbox.start(...) as sb: ...
    """

    def __init__(self, container_id: str, repo_mount: str, platform: str):
        self.container_id = container_id
        self.repo_mount = repo_mount
        self.platform = platform
        self._alive = True

    @classmethod
    def start(
        cls,
        base_image: str,
        repo_dir: Path,
        *,
        platform: str = "linux/amd64",
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: int = 600,
        on_phase=None,
    ) -> "DockerSandbox":
        if not is_docker_available():
            raise DockerError("Docker daemon is not running (or `docker` is not on PATH).")
        name = f"r2e-bootstrap-{uuid.uuid4().hex[:8]}"
        # Pull first so the start-up isn't conflated with image-not-found errors.
        if on_phase is not None:
            try:
                on_phase("pull_start", {"detail": base_image})
            except Exception:
                pass

        # Wire progress lines through on_phase so the UI doesn't look frozen
        # during long base-image pulls (golang:1.23 etc. can take 30–60s).
        def _pull_progress(line: str) -> None:
            if on_phase is None:
                return
            try:
                on_phase("pull_progress", {"detail": line})
            except Exception:
                pass

        pull = pull_image_streaming(
            base_image, platform=platform, timeout=timeout,
            on_progress=_pull_progress if on_phase is not None else None,
        )
        if not pull.ok:
            raise DockerError(f"failed to pull {base_image!r}: {pull.stderr.strip()[:400]}")
        if on_phase is not None:
            try:
                on_phase("pull_done", {"detail": base_image})
                on_phase("sandbox_start", {"detail": name})
            except Exception:
                pass
        # IMPORTANT: we COPY the repo into the container instead of bind-mounting.
        # Bind mounts aren't captured by `docker commit`, so a bind-mounted /workspace
        # would leave the committed image with an empty repo dir. We want the repo
        # baked into the image's filesystem.
        args = [
            "docker", "run", "-d",
            "--platform", platform,
            "--name", name,
            "-w", workdir,
        ]
        for k, v in (env or {}).items():
            args.extend(["-e", f"{k}={v}"])
        args.extend([base_image, "sleep", "infinity"])
        result = _run(args, timeout=60)
        if not result.ok:
            raise DockerError(f"docker run failed: {result.stderr.strip()[:400]}")
        cid = result.stdout.strip()
        if not cid:
            raise DockerError("docker run did not return a container id")
        # Create workdir + copy repo contents into it
        mk = _run(["docker", "exec", cid, "mkdir", "-p", workdir], timeout=15)
        if not mk.ok:
            _run(["docker", "rm", "-f", cid], timeout=10)
            raise DockerError(f"mkdir {workdir} failed: {mk.stderr.strip()[:400]}")
        # `docker cp <src>/. <cid>:<dest>` copies contents (not the dir itself)
        cp = _run(
            ["docker", "cp", f"{repo_dir.resolve()}/.", f"{cid}:{workdir}"],
            timeout=180,
        )
        if not cp.ok:
            _run(["docker", "rm", "-f", cid], timeout=10)
            raise DockerError(f"docker cp into container failed: {cp.stderr.strip()[:400]}")
        if on_phase is not None:
            try:
                on_phase("sandbox_done", {"detail": name})
            except Exception:
                pass
        return cls(container_id=cid, repo_mount=str(repo_dir.resolve()), platform=platform)

    def exec(self, command: str, *, timeout: int = 300) -> ExecResult:
        """Run a shell command inside the container. State persists across calls."""
        if not self._alive:
            raise DockerError("sandbox has been cleaned up; cannot exec")
        # Wrap in bash -lc so users can write multi-line / piped commands.
        args = ["docker", "exec", self.container_id, "bash", "-lc", command]
        return _run(args, timeout=timeout)

    def read_file(self, path: str, *, max_bytes: int = 50_000) -> str:
        """Read a file path inside the container, capped at max_bytes."""
        r = self.exec(f"head -c {max_bytes} {shlex.quote(path)}", timeout=30)
        if not r.ok:
            raise DockerError(f"read_file({path!r}) failed: {r.stderr.strip()[:200]}")
        return r.stdout

    def list_dir(self, path: str = ".") -> list[str]:
        r = self.exec(f"ls -1A {shlex.quote(path)} | head -200", timeout=10)
        if not r.ok:
            return []
        return [line for line in r.stdout.splitlines() if line.strip()]

    def commit(self, tag: str, *, message: str = "repo2rlenv bootstrap") -> str:
        """Commit the container to an image. Returns the image's content digest."""
        if not self._alive:
            raise DockerError("sandbox has been cleaned up; cannot commit")
        r = _run(["docker", "commit", "-m", message, self.container_id, tag], timeout=120)
        if not r.ok:
            raise DockerError(f"docker commit failed: {r.stderr.strip()[:400]}")
        # Resolve the image's RepoDigests (only present after a push) OR the local Id
        inspect = _run(["docker", "image", "inspect", tag, "--format", "{{json .}}"], timeout=10)
        if inspect.ok:
            try:
                data = json.loads(inspect.stdout)
                if isinstance(data, list):
                    data = data[0] if data else {}
                digests = data.get("RepoDigests") or []
                if digests:
                    return digests[0]
                return data.get("Id", tag)
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        return tag

    def push(self, tag: str, *, timeout: int = 900) -> bool:
        r = _run(["docker", "push", tag], timeout=timeout)
        if not r.ok:
            logger.warning("docker push failed for %s: %s", tag, r.stderr.strip()[:400])
            return False
        return True

    def cleanup(self) -> None:
        if not self._alive:
            return
        # `rm -f` stops + removes in one go
        _run(["docker", "rm", "-f", self.container_id], timeout=30)
        self._alive = False

    def __enter__(self) -> "DockerSandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()
