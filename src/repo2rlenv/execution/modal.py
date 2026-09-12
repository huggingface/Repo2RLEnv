"""Modal VM generation worker. Docker and task processes execute remotely."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from repo2rlenv.execution.base import CommandResult, WorkerSpec

APP_NAME = "repo2rlenv-owned-generation"


class ModalWorker:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.id = sandbox.object_id

    @classmethod
    def create(cls, spec: WorkerSpec) -> ModalWorker:
        import modal

        image = modal.Image.from_registry(spec.image or "ubuntu:24.04", add_python="3.12")
        image = (
            image.env({"DEBIAN_FRONTEND": "noninteractive"})
            .apt_install(
                "docker.io",
                "docker-buildx",
                "docker-compose-v2",
                "git",
                "curl",
                "ca-certificates",
                "python3-venv",
            )
            .pip_install("uv==0.10.9")
            .run_commands("mkdir -p /work /evidence", "python -m venv /opt/repo2rlenv")
            .env({"PATH": "/opt/repo2rlenv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"})
        )
        sandbox = modal.Sandbox.create(
            "/usr/bin/dockerd",
            "--storage-driver=overlay2",
            app=modal.App.lookup(APP_NAME, create_if_missing=True),
            name=spec.name,
            image=image,
            cpu=(spec.cpus, spec.cpus),
            memory=spec.memory_mb,
            timeout=spec.timeout_sec,
            experimental_options={"vm_runtime": True},
        )
        return cls(sandbox)

    @classmethod
    def connect(cls, worker_id: str) -> ModalWorker:
        import modal

        return cls(modal.Sandbox.from_id(worker_id))

    def exec(
        self, argv: list[str], *, timeout: int = 600, env: dict[str, str] | None = None
    ) -> CommandResult:
        import modal

        if not argv or timeout <= 0:
            raise ValueError("Remote execution needs an argv and positive timeout")
        process = self.sandbox.exec(
            *argv,
            timeout=timeout,
            secrets=[modal.Secret.from_dict(env)] if env else [],
        )
        # Drain both streams concurrently so a full stderr pipe cannot deadlock a
        # controller that is still reading stdout.
        with ThreadPoolExecutor(max_workers=2) as pool:
            stdout = pool.submit(process.stdout.read)
            stderr = pool.submit(process.stderr.read)
            process.wait()
            return CommandResult(process.returncode, stdout.result(), stderr.result())

    def upload(self, local: Path, remote: str) -> None:
        if not local.is_file() or local.is_symlink():
            raise ValueError("Upload requires a regular local file")
        self.sandbox.filesystem.copy_from_local(str(local), remote)

    def download(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self.sandbox.filesystem.copy_to_local(remote, str(local))

    def terminate(self) -> None:
        self.sandbox.terminate(wait=True)
