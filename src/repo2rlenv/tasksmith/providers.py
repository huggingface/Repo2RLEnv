"""Remote author workspaces; Harbor remains the task execution engine.

Creation is never retried here. The caller must durably claim the operation and
reserve its budget through ``on_receipt`` before allowing a provider effect.
The callback must persist every receipt before returning. An uncertain receipt
retains the cleanup obligation; it is not permission to settle a reservation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import shlex
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal


class ProviderError(RuntimeError):
    pass


class DeadlineExceeded(ProviderError):
    pass


class CleanupUncertain(ProviderError):
    """Keep the resource identity and reservation until reconciliation succeeds."""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or value == "."
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError(f"Unsafe relative file path: {value!r}")
    return value


def _absolute(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError("Remote paths must be absolute")
    _relative(value[1:])
    return value


def _safe_local_parents(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"Symlink in local artifact path: {parent}")


@dataclass(frozen=True)
class BuildSpec:
    """Immutable build bytes or an explicitly typed provider image reference.

    Recipe files include Dockerfile; no local repository is implicitly uploaded.
    Native references are provider-specific and carry the caller's build identity.
    """

    kind: Literal["recipe", "oci", "modal-image", "daytona-snapshot"]
    role: Literal["author", "bootstrap", "solver", "grader", "reference"]
    reference: str = ""
    files: tuple[tuple[str, bytes], ...] = ()
    source_sha: str | None = None
    executables: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "files", tuple((name, bytes(data)) for name, data in self.files))
        if self.kind not in {"recipe", "oci", "modal-image", "daytona-snapshot"}:
            raise ValueError("Unsupported build reference kind")
        if self.role not in {"author", "bootstrap", "solver", "grader", "reference"}:
            raise ValueError("Unsupported build role")
        if self.source_sha and not re.fullmatch(r"[0-9a-f]{40}", self.source_sha):
            raise ValueError("Source identity must be a full Git SHA")
        names = [_relative(name) for name, _ in self.files]
        object.__setattr__(self, "executables", tuple(sorted(set(self.executables))))
        if not set(self.executables).issubset(names):
            raise ValueError("Executable build paths must name context files")
        if len(names) != len(set(names)) or len(names) > 500:
            raise ValueError("Duplicate or excessive build files")
        if sum(len(data) for _, data in self.files) > 20_000_000:
            raise ValueError("Build context exceeds 20 MB")
        for name in names:
            if any(str(p) in names for p in PurePosixPath(name).parents if str(p) != "."):
                raise ValueError("Overlapping build file paths")
        if self.kind == "recipe":
            if self.reference or "Dockerfile" not in names:
                raise ValueError("Recipe requires Dockerfile bytes and no external reference")
        elif self.files or not self.reference:
            raise ValueError("Image reference must be nonempty and have no context files")
        if self.kind == "oci" and not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", self.reference):
            raise ValueError("OCI image must use an immutable SHA-256 digest")
        if self.kind == "modal-image" and not self.reference.startswith("im-"):
            raise ValueError("Modal image reference must be an im-* ID")

    @property
    def digest(self) -> str:
        return _digest(
            {
                "kind": self.kind,
                "role": self.role,
                "reference": self.reference,
                "source_sha": self.source_sha,
                "files": sorted((p, hashlib.sha256(b).hexdigest()) for p, b in self.files),
                "executables": self.executables,
            }
        )

    @classmethod
    def from_directory(cls, path: Path, *, role: str, source_sha: str | None = None):
        """Read build inputs as data, rejecting links/devices throughout the tree."""
        _safe_local_parents(path)
        if not path.is_dir():
            raise ValueError("Build context must be a directory")
        files = []
        executables = []
        total = 0
        for p in sorted(path.rglob("*")):
            mode = p.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f"Nonregular build input: {p}")
            size = p.stat().st_size
            total += size
            if len(files) >= 500 or total > 20_000_000:
                raise ValueError("Build context exceeds limits")
            with p.open("rb") as stream:
                data = stream.read(size + 1)
            if len(data) != size:
                raise ValueError("Build input changed while reading")
            files.append((p.relative_to(path).as_posix(), data))
            if mode & 0o111:
                executables.append(p.relative_to(path).as_posix())
        return cls(
            kind="recipe",
            role=role,
            files=tuple(files),
            source_sha=source_sha,
            executables=tuple(executables),
        )


@dataclass(frozen=True)
class WorkspaceConfig:
    provider: Literal["modal", "daytona"]
    operation_id: str
    deadline: float
    build: BuildSpec
    profile: str = "cpu-direct"
    cpus: int = 2
    memory_mib: int = 4096
    disk_gib: int = 10
    network: Literal["public", "none"] = "public"
    app_name: str = "repo2rlenv-tasksmith"
    cleanup_timeout_sec: int = 30
    max_file_bytes: int = 2_000_000
    max_export_bytes: int = 20_000_000
    max_export_files: int = 150
    require_native_deadline: bool = False

    def __post_init__(self):
        if self.provider not in {"modal", "daytona"} or self.profile != "cpu-direct":
            raise ValueError("Only Modal/Daytona CPU direct workspaces are supported")
        if not self.operation_id or not math.isfinite(self.deadline):
            raise ValueError("Operation identity and absolute deadline are required")
        if self.network not in {"public", "none"}:
            raise ValueError("Unsupported network policy")
        if not (1 <= self.cpus <= 8 and 1024 <= self.memory_mib <= 32768):
            raise ValueError("CPU profile resources exceed supported bounds")
        if not 1 <= self.disk_gib <= 100 or not 1 <= self.cleanup_timeout_sec <= 60:
            raise ValueError("Invalid disk or cleanup bound")
        if not (1 <= self.max_file_bytes <= self.max_export_bytes <= 20_000_000):
            raise ValueError("Invalid transfer byte limits")
        if not 1 <= self.max_export_files <= 500:
            raise ValueError("Invalid export count limit")
        if self.provider == "daytona" and self.memory_mib % 1024:
            raise ValueError("Daytona memory must be whole GiB")
        if self.build.kind == "modal-image" and self.provider != "modal":
            raise ValueError("Modal image cannot be used on Daytona")
        if self.build.kind == "daytona-snapshot" and self.provider != "daytona":
            raise ValueError("Daytona snapshot cannot be used on Modal")

    @property
    def digest(self) -> str:
        values = asdict(self)
        values["build"] = self.build.digest
        return _digest(values)

    @property
    def labels(self) -> dict[str, str]:
        return {"tasksmith.config": self.digest, "tasksmith.operation": _digest(self.operation_id)}


@dataclass(frozen=True)
class ResourceReceipt:
    provider: str
    operation_id: str
    config_digest: str
    build_digest: str
    build_role: str
    profile: str
    deadline: float
    resource_id: str | None
    image_id: str | None
    status: str
    observed_at: float
    deadline_enforcement: str
    requested_cpus: int
    requested_memory_mib: int
    requested_disk_gib: int | None
    exposure_tail_sec: int
    detail: str = ""


ReceiptCallback = Callable[[ResourceReceipt], Awaitable[None]]


class ModalBackend:
    """The pinned Modal SDK; injectable SDK object supports network-free tests."""

    deadline_enforcement = "provider-timeout+controller-watchdog"

    def __init__(self, sdk=None):
        if sdk is None:
            import modal as sdk
        self.sdk = sdk
        self.handle = None
        self.image_id = None

    def preflight(self, config):
        pass

    async def create(self, config, context, remaining, built):
        app = await self.sdk.App.lookup.aio(config.app_name, create_if_missing=True)
        spec = config.build
        if spec.kind == "modal-image":
            image = self.sdk.Image.from_id(spec.reference)
        elif spec.kind == "oci":
            image = self.sdk.Image.from_registry(spec.reference)
        else:
            image = self.sdk.Image.from_dockerfile(
                context / "Dockerfile", context_dir=context, ignore=[]
            )
        image = await image.build.aio(app)
        self.image_id = image.object_id
        await built(self.image_id)
        self.handle = await self.sdk.Sandbox.create.aio(
            "sleep",
            "infinity",
            app=app,
            image=image,
            timeout=remaining(),
            cpu=config.cpus,
            memory=config.memory_mib,
            workdir="/",
            block_network=config.network == "none",
            tags=config.labels,
        )
        return self.handle.object_id

    async def attach(self, resource_id):
        self.handle = await self.sdk.Sandbox.from_id.aio(resource_id)
        return await self.handle.get_tags.aio()

    async def running(self):
        return await self.handle.poll.aio() is None

    async def execute(self, command, timeout):
        process = await self.handle.exec.aio("bash", "-lc", command, timeout=timeout)
        out, err = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
        return await process.wait.aio(), out, err

    async def terminate(self):
        await self.handle.terminate.aio()

    async def terminal(self, resource_id):
        from modal.exception import NotFoundError

        try:
            handle = await self.sdk.Sandbox.from_id.aio(resource_id)
        except NotFoundError:
            return True
        return await handle.poll.aio() is not None


class DaytonaBackend:
    """SDK 0.198 has inactivity stop, not wall-clock TTL. Never imply otherwise."""

    deadline_enforcement = "controller-watchdog+provider-inactivity"

    def __init__(self, sdk=None, client=None):
        if sdk is None:
            import daytona as sdk
        self.sdk = sdk
        self.client = client
        self.handle = None
        self.image_id = None

    def preflight(self, config):
        if config.require_native_deadline:
            raise ValueError(
                "Pinned Daytona SDK lacks server wall-clock TTL; controller recovery required"
            )

    async def create(self, config, context, remaining, built):
        self.client = self.client or self.sdk.AsyncDaytona()
        common = dict(
            labels=config.labels,
            public=False,
            ephemeral=True,
            auto_delete_interval=0,
            auto_stop_interval=1,
            network_block_all=config.network == "none",
        )
        if config.build.kind == "daytona-snapshot":
            snapshot = await self.client.snapshot.get(config.build.reference)
            if (snapshot.cpu, snapshot.mem, snapshot.disk, snapshot.gpu) != (
                config.cpus,
                config.memory_mib // 1024,
                config.disk_gib,
                0,
            ):
                raise ValueError("Snapshot resources do not match the requested CPU profile")
            params = self.sdk.CreateSandboxFromSnapshotParams(
                snapshot=config.build.reference, **common
            )
        else:
            image = (
                self.sdk.Image.base(config.build.reference)
                if config.build.kind == "oci"
                else self.sdk.Image.from_dockerfile(context / "Dockerfile")
            )
            # SDK COPY parsing must never cause files outside our frozen context to upload.
            if config.build.kind == "recipe":
                for item in image._context_list:
                    if not Path(item.source_path).resolve().is_relative_to(context.resolve()):
                        raise ValueError("Dockerfile COPY escapes frozen build context")
            params = self.sdk.CreateSandboxFromImageParams(
                image=image,
                resources=self.sdk.Resources(
                    cpu=config.cpus, memory=config.memory_mib // 1024, disk=config.disk_gib, gpu=0
                ),
                **common,
            )
        self.handle = await self.client.create(params=params, timeout=remaining())
        self.image_id = self.handle.snapshot
        return self.handle.id

    async def attach(self, resource_id):
        self.client = self.client or self.sdk.AsyncDaytona()
        self.handle = await self.client.get(resource_id)
        self.image_id = self.handle.snapshot
        return self.handle.labels

    async def running(self):
        return getattr(self.handle.state, "value", self.handle.state) == "started"

    async def execute(self, command, timeout):
        result = await self.handle.process.exec(command, timeout=timeout)
        return result.exit_code, result.result or "", ""

    async def terminate(self):
        await self.client.delete(self.handle)

    async def terminal(self, resource_id):
        from daytona.common.errors import DaytonaNotFoundError

        try:
            handle = await self.client.get(resource_id)
        except DaytonaNotFoundError:
            return True
        # Stopped sandboxes may still incur storage charges: require destruction.
        return getattr(handle.state, "value", handle.state) == "destroyed"


# Executed only in the remote workspace, never imported/evaluated on the host.
_FILE_SCRIPT = r"""
import os, stat, json, sys, base64, hashlib
arg = json.loads(sys.argv[1])
def parent(path, create=False):
    parts = path.split('/')[1:]
    assert parts and all(p not in ('', '.', '..') for p in parts)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            if create:
                try: os.mkdir(part, dir_fd=fd)
                except FileExistsError: pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = nxt
        return fd, parts[-1]
    except BaseException:
        os.close(fd); raise
def read(path, limit):
    directory, name = parent(path)
    try: fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally: os.close(directory)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        assert stat.S_ISREG(before.st_mode) and before.st_size <= limit, 'nonregular/oversized file'
        data = stream.read(limit+1)
        after = os.fstat(stream.fileno())
        assert len(data) <= limit and (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino), 'file changed during export'
        return data
if arg['op'] == 'write':
    directory, name = parent(arg['path'], True)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=directory)
    finally: os.close(directory)
    with os.fdopen(fd, 'r+b') as stream:
        assert stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'not a regular file'
        if arg['offset'] == 0: stream.truncate(0)
        else: assert os.fstat(stream.fileno()).st_size == arg['offset'], 'write changed'
        stream.seek(arg['offset']); stream.write(base64.b64decode(arg['data'], validate=True))
    print('{}')
elif arg['op'] == 'read':
    print(json.dumps({'data': base64.b64encode(read(arg['path'], arg['limit'])).decode()}))
else:
    root = arg['path']
    directory, name = parent(root)
    try: fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    finally: os.close(directory)
    os.close(fd)
    def inventory():
        entries = []
        for here, dirs, files in os.walk(root, followlinks=False):
            for name in sorted(dirs + files):
                p = os.path.join(here, name); s = os.lstat(p)
                assert stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode), 'nonregular export entry'
                entries.append((p, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino))
                assert len(entries) <= arg['count']*4, 'too many export entries'
        return sorted(entries)
    before = inventory(); output = []; total = 0
    for p, mode, *_ in before:
        if not stat.S_ISREG(mode): continue
        data = read(p, arg['file_limit']); total += len(data)
        assert len(output) < arg['count'] and total <= arg['total_limit'], 'export too large'
        output.append({'path': os.path.relpath(p, root), 'sha256': hashlib.sha256(data).hexdigest(), 'executable': bool(mode & 0o111),
                       'data': base64.b64encode(data).decode()})
    assert before == inventory(), 'export tree changed'
    print(json.dumps(output))
"""

_SHELL_SCRIPT = r"""
import json, subprocess, sys, tempfile, os, signal, time
arg=json.loads(sys.argv[1]); started=time.monotonic()
with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
    p=subprocess.Popen(['bash','-lc',arg['command']], stdout=out, stderr=err, start_new_session=True)
    timed_out=False
    try: code=p.wait(timeout=arg['timeout'])
    except subprocess.TimeoutExpired:
        timed_out=True; os.killpg(p.pid, signal.SIGKILL); code=p.wait()
    def tail(f,n):
        size=f.tell(); f.seek(max(0,size-n)); return f.read(n).decode('utf-8','replace')
    print(json.dumps({'exit_code':code,'stdout':tail(out,20000),'stderr':tail(err,4000),
                      'timed_out':timed_out,'effective_timeout_sec':arg['timeout'],
                      'elapsed_seconds':round(time.monotonic()-started,3)}))
"""


class RemoteWorkspace:
    """Async AuthorSandbox-compatible facade, with explicit durable lifecycle.

    ``stop`` remains available after the work deadline for bounded cleanup only.
    The caller owns budget reservations, recovery of uncertain receipts, and
    provider-specific image/storage charges. No automatic paid retries occur.
    """

    def __init__(self, config: WorkspaceConfig, on_receipt: ReceiptCallback, *, backend=None):
        self.config, self.on_receipt = config, on_receipt
        self.backend = backend
        self.resource_id = None
        self.receipt = None
        self._persisted_status = None
        self._attempted = False
        self._ready = False
        self._watchdog = None
        self._creation_task = None
        self._abandoned = False
        self._emergency_cleanup = None
        self._stop_lock = asyncio.Lock()

    def _remaining(self, requested=86_400):
        value = min(requested, math.floor(self.config.deadline - time.time()))
        if value < 1:
            raise DeadlineExceeded("Workspace absolute deadline exhausted")
        return value

    def _backend(self):
        if self.backend is None:
            self.backend = ModalBackend() if self.config.provider == "modal" else DaytonaBackend()
        self.backend.preflight(self.config)
        return self.backend

    async def _record(self, status, detail=""):
        self.receipt = ResourceReceipt(
            self.config.provider,
            self.config.operation_id,
            self.config.digest,
            self.config.build.digest,
            self.config.build.role,
            self.config.profile,
            self.config.deadline,
            self.resource_id,
            getattr(self.backend, "image_id", None),
            status,
            time.time(),
            self.backend.deadline_enforcement,
            self.config.cpus,
            self.config.memory_mib,
            self.config.disk_gib if self.config.provider == "daytona" else None,
            (60 if self.config.provider == "daytona" else 0) + self.config.cleanup_timeout_sec,
            detail,
        )
        await self.on_receipt(self.receipt)
        self._persisted_status = status
        return self.receipt

    async def start(self):
        self._remaining()
        self._backend()
        if self._attempted:
            raise ProviderError("Creation already attempted; reconcile the retained receipt")
        self._attempted = True
        await self._record("claimed")
        self._creation_task = asyncio.create_task(self._create())
        try:
            async with asyncio.timeout(self._remaining()):
                await asyncio.shield(self._creation_task)
        except BaseException as exc:
            self._abandoned = True
            self._ready = False
            if self._persisted_status != "terminated":
                await self._record("uncertain", type(exc).__name__)
            # Keep the creation coroutine alive long enough to capture a returned ID
            # and immediately clean it up. Its SDK request retains the original timeout.
            self._creation_task.add_done_callback(self._consume_creation_exception)
            raise
        return self

    @staticmethod
    def _consume_creation_exception(task):
        if not task.cancelled():
            task.exception()

    async def _create(self):
        try:
            with tempfile.TemporaryDirectory(prefix="tasksmith-build-") as tmp:
                context = Path(tmp)
                for name, data in self.config.build.files:
                    path = context / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    path.chmod(0o755 if name in self.config.build.executables else 0o644)

                async def built(image_id):
                    await self._record("image-ready")

                async with asyncio.timeout(self._remaining()):
                    self.resource_id = await self.backend.create(
                        self.config, context, self._remaining, built
                    )
                self._watchdog = asyncio.create_task(self._deadline_stop())
                await self._record("submitted")
                if self._abandoned or time.time() >= self.config.deadline:
                    await self.stop()
                    return
                self._remaining()
                self._ready = True
                await self._record("running")
        except BaseException as exc:
            self._ready = False
            if self.resource_id:
                self._emergency_cleanup = asyncio.create_task(self._cleanup_after_failure())
            try:
                await self._record("uncertain", type(exc).__name__)
            except Exception:
                pass
            raise

    async def _cleanup_after_failure(self):
        try:
            await self.stop()
        except CleanupUncertain:
            pass

    async def attach(self, resource_id: str, *, cleanup_only: bool = False):
        timeout = self.config.cleanup_timeout_sec if cleanup_only else self._remaining(30)
        self._backend()
        if self._attempted or not resource_id:
            raise ProviderError("Cannot attach after a creation/attachment attempt")
        self._attempted = True
        # Do not authorize deletion until the retained identity matches.
        async with asyncio.timeout(timeout):
            labels = await self.backend.attach(resource_id)
            if any(labels.get(k) != v for k, v in self.config.labels.items()):
                raise ProviderError("Resource identity/configuration mismatch")
            self.resource_id = resource_id
            self._watchdog = asyncio.create_task(self._deadline_stop())
            await self._record("attached")
            if not cleanup_only and not await self.backend.running():
                raise ProviderError("Retained workspace is not running; no implicit restart")
        self._ready = not cleanup_only
        return self

    async def _deadline_stop(self):
        await asyncio.sleep(max(0, self.config.deadline - time.time()))
        try:
            await self.stop()
        except CleanupUncertain:
            pass  # Uncertain receipt is durable; recovery belongs to the controller.

    async def _run(self, script, arg, *, timeout=120, output_limit=40_000):
        if not self._ready:
            raise ProviderError("Workspace is not ready")
        effective = self._remaining(timeout)
        command = "python3 -c " + shlex.quote(script) + " " + shlex.quote(json.dumps(arg))
        if len(command.encode()) > 120_000:
            raise ValueError("Encoded command exceeds remote argument limit")
        async with asyncio.timeout(effective):
            code, out, err = await self.backend.execute(command, effective)
        if code != 0:
            raise ProviderError(f"Remote helper failed ({code}): {(err or out)[-4000:]}")
        if len(out.encode()) > output_limit:
            raise ProviderError("Remote helper output exceeds transfer bound")
        return json.loads(out)

    async def shell(self, command: str, timeout_sec: int = 120) -> str:
        if not isinstance(command, str) or len(command.encode()) > 64_000:
            raise ValueError("Command exceeds 64 KB")
        timeout = self._remaining(max(1, min(timeout_sec, 3600)))
        if timeout < 2:
            raise DeadlineExceeded("Insufficient time for command and response")
        result = await self._run(
            _SHELL_SCRIPT,
            {"command": command, "timeout": timeout - 1},
            timeout=timeout,
            output_limit=150_000,
        )
        return json.dumps(result)

    async def write(self, path: str, text: str | bytes) -> None:
        _absolute(path)
        data = text.encode() if isinstance(text, str) else text
        if len(data) > self.config.max_file_bytes:
            raise ValueError("File exceeds transfer limit")
        for offset in range(0, max(1, len(data)), 24_000):
            await self._run(
                _FILE_SCRIPT,
                {
                    "op": "write",
                    "path": path,
                    "offset": offset,
                    "data": base64.b64encode(data[offset : offset + 24_000]).decode(),
                },
            )

    async def read(self, path: str) -> bytes:
        _absolute(path)
        result = await self._run(
            _FILE_SCRIPT,
            {"op": "read", "path": path, "limit": self.config.max_file_bytes},
            output_limit=self.config.max_file_bytes * 2 + 1000,
        )
        data = base64.b64decode(result["data"], validate=True)
        if len(data) > self.config.max_file_bytes:
            raise ValueError("File exceeds transfer limit")
        return data

    async def prepare(self, source: dict) -> None:
        repo, base, head = source["repo"], source["base_sha"], source["head_sha"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
            raise ValueError("Expected canonical GitHub owner/repository")
        if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
            raise ValueError("Preparation requires full pinned base/head Git SHAs")
        if self.config.network != "public":
            raise ValueError("Pinned source hydration requires a network-enabled author workspace")
        command = (
            "set -eu; mkdir -p /private /output/task /workspace; "
            "test ! -e /workspace/repo; "
            f"git clone --filter=blob:none --no-checkout {shlex.quote('https://github.com/' + repo + '.git')} /workspace/repo; "
            f"cd /workspace/repo; git fetch origin {base} {head}; "
            f'test "$(git rev-parse {base}^{{commit}})" = {base}; '
            f'test "$(git rev-parse {head}^{{commit}})" = {head}; '
            f"git checkout --detach {base}; git diff --binary {base} {head} > /private/gold.patch"
        )
        result = json.loads(await self.shell(command, 300))
        if result["exit_code"]:
            raise ProviderError(f"Pinned repository preparation failed: {result}")
        await self.write("/private/pr.json", json.dumps(source, indent=2))

    async def export(self, destination: Path, remote_path: str = "/output/task") -> list[dict]:
        _absolute(remote_path)
        _safe_local_parents(destination)
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise ValueError("Export destination must be absent or an empty regular directory")
        cfg = self.config
        entries = await self._run(
            _FILE_SCRIPT,
            {
                "op": "export",
                "path": remote_path,
                "count": cfg.max_export_files,
                "file_limit": cfg.max_file_bytes,
                "total_limit": cfg.max_export_bytes,
            },
            output_limit=cfg.max_export_bytes * 2 + cfg.max_export_files * 2000,
        )
        if not isinstance(entries, list) or len(entries) > cfg.max_export_files:
            raise ValueError("Invalid export manifest")
        decoded = {}
        executable = {}
        total = 0
        for row in entries:
            name = _relative(row["path"])
            data = base64.b64decode(row["data"], validate=True)
            total += len(data)
            if name in decoded or len(data) > cfg.max_file_bytes or total > cfg.max_export_bytes:
                raise ValueError("Duplicate or oversized exported file")
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Export hash mismatch")
            flag = row.get("executable", False)
            if not isinstance(flag, bool):
                raise ValueError("Invalid executable-file flag")
            decoded[name] = data
            executable[name] = flag
        if any(str(p) in decoded for n in decoded for p in PurePosixPath(n).parents):
            raise ValueError("Overlapping exported file paths")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".tasksmith-export-", dir=destination.parent
        ) as tmp:
            staging = Path(tmp) / "task"
            staging.mkdir()
            for name, data in decoded.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o755 if executable[name] else 0o644)
            _safe_local_parents(destination)
            os.replace(staging, destination)
        return [
            {
                "path": n,
                "size": len(b),
                "sha256": hashlib.sha256(b).hexdigest(),
                "executable": executable[n],
            }
            for n, b in sorted(decoded.items())
        ]

    async def stop(self) -> ResourceReceipt:
        async with self._stop_lock:
            self._abandoned = True
            self._ready = False
            if self._watchdog and self._watchdog is not asyncio.current_task():
                self._watchdog.cancel()
            if not self.resource_id:
                raise CleanupUncertain("No resource ID: reconcile the original creation operation")
            if self.receipt and self._persisted_status == "terminated":
                return self.receipt
            try:
                await self._record("stopping")
            except Exception:
                pass  # Journal failure must not prevent terminating a known resource.
            try:
                async with asyncio.timeout(self.config.cleanup_timeout_sec):
                    try:
                        await self.backend.terminate()
                    except Exception:
                        pass  # A fresh terminal observation, never this request, establishes success.
                    while not await self.backend.terminal(self.resource_id):
                        await asyncio.sleep(0.25)
                return await self._record("terminated")
            except BaseException as exc:
                try:
                    await self._record("uncertain", f"cleanup: {type(exc).__name__}")
                except Exception:
                    pass
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise CleanupUncertain(f"Unconfirmed cleanup for {self.resource_id}") from exc
