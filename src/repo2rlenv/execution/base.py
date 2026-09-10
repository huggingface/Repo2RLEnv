"""Small provider contract for remote commands and files, with no local fallback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class WorkerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["modal", "daytona"] = "modal"
    cpus: int = Field(default=2, ge=1, le=16)
    memory_mb: int = Field(default=4096, ge=1024, le=65536)
    disk_gb: int = Field(default=10, ge=5, le=100)
    timeout_sec: int = Field(default=3600, ge=60, le=14400)
    image: str | None = None
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,60}$")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""

    def checked(self, context: str) -> CommandResult:
        if self.returncode:
            raise RemoteCommandError(context, self)
        return self


class RemoteCommandError(RuntimeError):
    def __init__(self, context: str, result: CommandResult):
        # Logs belong in the evidence store; arbitrary command output may contain
        # credentials or private task context and must not enter exception text.
        super().__init__(f"{context} failed with exit code {result.returncode}")
        self.result = result


class RemoteWorker(Protocol):
    id: str

    def exec(
        self, argv: list[str], *, timeout: int = 600, env: dict[str, str] | None = None
    ) -> CommandResult: ...

    def upload(self, local: Path, remote: str) -> None: ...

    def download(self, remote: str, local: Path) -> None: ...

    def terminate(self) -> None: ...


def connect_worker(provider: str, worker_id: str) -> RemoteWorker:
    if provider == "modal":
        from repo2rlenv.execution.modal import ModalWorker

        return ModalWorker.connect(worker_id)
    if provider == "daytona":
        from repo2rlenv.execution.daytona import DaytonaWorker

        return DaytonaWorker.connect(worker_id)
    raise ValueError(f"Unsupported remote provider {provider!r}")


def create_worker(spec: WorkerSpec) -> RemoteWorker:
    if spec.provider == "modal":
        from repo2rlenv.execution.modal import ModalWorker

        return ModalWorker.create(spec)
    from repo2rlenv.execution.daytona import DaytonaWorker

    return DaytonaWorker.create(spec)
