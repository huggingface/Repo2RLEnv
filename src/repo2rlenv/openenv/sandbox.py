"""The Docker sandbox a Repo2RLEnv task runs in.

Every runtime pipeline we ship bakes the repository's state into the task's own
image, so the sandbox is Docker — there is no meaningful "run it locally" mode
for a task whose starting state only exists inside a container.

The sandbox reproduces the filesystem contract our verifiers are written
against:

    /workspace       the checkout the agent edits (the image's WORKDIR)
    /tests           the task's tests/, staged just before the verifier runs
    /solution        the task's solution/, staged only for the oracle
    /logs/verifier   where test.sh writes reward.txt + reward-details.json
    /logs/agent      scratch space

Files are streamed in over the Docker API rather than bind-mounted, so the
environment server can itself run inside a container.
"""

from __future__ import annotations

import io
import logging
import posixpath
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

from repo2rlenv.openenv.dataset import (
    NETWORK_ALLOWLIST,
    NETWORK_NONE,
    Repo2RLEnvTask,
)
from repo2rlenv.openenv.reward import RewardReport, read_reward

logger = logging.getLogger(__name__)

#: Exit code GNU `timeout` reports when it kills a command.
TIMEOUT_EXIT_CODE = 124

#: Where our pipelines put the checkout. Used only when the image itself
#: declares no WORKDIR.
DEFAULT_WORKDIR = "/workspace"


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot start or cannot run something."""


@dataclass(frozen=True)
class ExecResult:
    """Outcome of one command run inside the sandbox."""

    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class SandboxPaths:
    """The absolute paths our task scripts are written against."""

    workdir: str = DEFAULT_WORKDIR
    tests: str = "/tests"
    solution: str = "/solution"
    logs: str = "/logs"

    @property
    def logs_verifier(self) -> str:
        return posixpath.join(self.logs, "verifier")

    @property
    def logs_agent(self) -> str:
        return posixpath.join(self.logs, "agent")

    def as_env(self, *, agent_visible: bool = False) -> dict[str, str]:
        """Path layout exported to task scripts.

        Harbor-lineage scripts read `TEST_DIR`; ours use absolute paths, but
        exporting both keeps third-party tasks working unchanged.

        Args:
            agent_visible: return only the paths an agent legitimately needs.
                The verifier and oracle directories are withheld — that is where
                the grading logic and the answer live, and only the verifier and
                oracle themselves need to be told where they are.
        """
        agent_paths = {
            "HARBOR_WORKDIR": self.workdir,
            "HARBOR_AGENT_LOGS_DIR": self.logs_agent,
        }
        if agent_visible:
            return agent_paths
        return {
            **agent_paths,
            "HARBOR_TESTS_DIR": self.tests,
            "HARBOR_SOLUTION_DIR": self.solution,
            "HARBOR_LOGS_DIR": self.logs_verifier,
            "TEST_DIR": self.tests,
        }


class DockerSandbox:
    """Runs one task inside its own image for the life of one episode.

    Args:
        keep_container: leave the container in place after `close()`, for debugging.
    """

    def __init__(self, keep_container: bool = False) -> None:
        self.agent_user: str | None = None
        self.paths = SandboxPaths()
        self.keep_container = keep_container
        self._client = None
        self._container = None
        self._image = ""
        self._has_timeout: bool | None = None

    @property
    def image(self) -> str:
        """The image backing this sandbox."""
        return self._image

    # --- lifecycle ----------------------------------------------------------

    def start(self, task: Repo2RLEnvTask) -> None:
        """Resolve the task's image, boot a container, and lay out the paths."""
        client = self._connect()
        self._image = self._resolve_image(task, client)
        self._container = client.containers.run(
            image=self._image,
            command=["sleep", "infinity"],
            detach=True,
            labels={"repo2rlenv.task": task.task_id},
            auto_remove=False,
            **_container_limits(task),
        )
        self.agent_user = task.agent_user
        self.paths = SandboxPaths(workdir=task.declared_workdir or self._image_workdir(client))
        self.mkdirs(self.paths.workdir, self.paths.logs_verifier, self.paths.logs_agent)
        # Those directories belong to the image's default account. A task that
        # asks for [agent].user would otherwise get a working directory it
        # cannot write to, breaking every edit the agent makes.
        self.chown(task.agent_user, self.paths.workdir, self.paths.logs_agent)

    def close(self) -> None:
        """Remove the container."""
        if self._container is not None and not self.keep_container:
            try:
                self._container.remove(force=True)
            except Exception:  # pragma: no cover - best-effort teardown
                logger.warning("could not remove container", exc_info=True)
        self._container = None
        self._client = None

    # --- primitives ---------------------------------------------------------

    def exec(
        self,
        command: str,
        *,
        timeout_s: float,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        user: str | None = None,
        agent_visible: bool = False,
    ) -> ExecResult:
        """Run `command` through bash and return its merged output.

        Args:
            user: account to run as, from `[agent].user` / `[verifier].user`.
            agent_visible: whether this is the agent's own command. Agent
                commands are not told where the verifier and oracle live.
        """
        container = self._require_container()
        process_env = dict(self.paths.as_env(agent_visible=agent_visible))
        process_env.update(env or {})
        exit_code, output = container.exec_run(
            cmd=["bash", "-c", self._with_timeout(command, timeout_s)],
            workdir=workdir or self.paths.workdir,
            environment=process_env,
            user=user or "",
            demux=False,
        )
        text = output.decode("utf-8", errors="replace") if output else ""
        # The Docker API reports a null exit code when the exec never started.
        status = int(exit_code) if exit_code is not None else 1
        return ExecResult(status, text, timed_out=status == TIMEOUT_EXIT_CODE)

    def read_text(self, path: str) -> str | None:
        """Read a text file, returning None when it does not exist."""
        result = self.exec(f"cat {_quote(path)}", timeout_s=60.0)
        return result.output if result.ok else None

    def write_text(self, path: str, content: str) -> None:
        """Write a text file, creating parent directories as needed."""
        container = self._require_container()
        directory, name = posixpath.split(path)
        self.mkdirs(directory)
        container.put_archive(directory, _tar_bytes({name: content.encode("utf-8")}))

    def upload_dir(self, source: Path, destination: str) -> None:
        """Copy a host directory into the container."""
        container = self._require_container()
        self.mkdirs(destination)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for entry in sorted(source.rglob("*")):
                # rglob yields directories as well as files, and `add` recurses
                # by default — leaving it on archives every nested file once
                # via its parent and again on its own iteration.
                archive.add(
                    entry,
                    arcname=entry.relative_to(source).as_posix(),
                    recursive=False,
                )
        container.put_archive(destination, stream.getvalue())

    def chown(self, user: str | None, *paths: str) -> None:
        """Hand `paths` to `user`, so a non-root phase can write to them.

        A no-op when the task declares no user. Runs as the image's default
        account, the only one able to change ownership.
        """
        if not user or not paths:
            return
        quoted = " ".join(_quote(path) for path in paths)
        result = self.exec(f"chown -R {_quote(user)} {quoted}", timeout_s=120.0, workdir="/")
        if not result.ok:
            raise SandboxError(
                f"could not give {user!r} ownership of {list(paths)}: {result.output.strip()}"
            )

    def mkdirs(self, *paths: str) -> None:
        """Create directories inside the container.

        Runs from `/` so it works before the working directory exists — a fresh
        image often declares a WORKDIR that is not in the filesystem yet.
        """
        quoted = " ".join(_quote(p) for p in paths if p)
        if not quoted:
            return
        result = self.exec(f"mkdir -p {quoted}", timeout_s=60.0, workdir="/")
        if not result.ok:
            raise SandboxError(f"could not create {list(paths)}: {result.output.strip()}")

    # --- episode phases -----------------------------------------------------

    def run_verifier(self, task: Repo2RLEnvTask) -> ExecResult:
        """Stage `tests/` and run `tests/test.sh`.

        Raises:
            SandboxError: if the task ships no verifier.
        """
        if task.test_script is None:
            raise SandboxError(
                f"task {task.task_id!r} has no tests/test.sh, so it cannot be graded"
            )
        # Start from an empty verifier log directory: the reward must describe
        # *this* run. The agent shares the container and could otherwise plant a
        # reward file that outlives a verifier which fails before writing one.
        self.exec(f"rm -rf {_quote(self.paths.logs_verifier)}", timeout_s=60.0, workdir="/")
        self.mkdirs(self.paths.logs_verifier)
        self._replace_dir(task.tests_dir, self.paths.tests)
        # Same reason as the working directory: a [verifier].user that cannot
        # write its reward file would make every such task unscorable.
        self.chown(task.verifier_user, self.paths.logs_verifier, self.paths.tests)
        script = posixpath.join(self.paths.tests, task.test_script.name)
        return self.exec(
            f"bash {_quote(script)}",
            timeout_s=task.verifier_timeout_s,
            user=task.verifier_user,
        )

    def run_solution(self, task: Repo2RLEnvTask) -> ExecResult:
        """Stage `solution/` and run `solution/solve.sh` — the oracle.

        Raises:
            SandboxError: if the task ships no oracle.
        """
        if task.solve_script is None:
            raise SandboxError(
                f"task {task.task_id!r} has no solution/solve.sh, so it has no oracle"
            )
        self._replace_dir(task.solution_dir, self.paths.solution)
        self.chown(task.agent_user, self.paths.solution)
        script = posixpath.join(self.paths.solution, task.solve_script.name)
        return self.exec(
            f"bash {_quote(script)}",
            timeout_s=task.agent_timeout_s,
            user=task.agent_user,
        )

    def _replace_dir(self, source: Path, destination: str) -> None:
        """Upload `source` over a freshly emptied `destination`.

        The agent shares the container and can pre-create these paths; anything
        it leaves behind must not survive into the verifier's view.
        """
        self.exec(f"rm -rf {_quote(destination)}", timeout_s=60.0, workdir="/")
        self.upload_dir(source, destination)

    def reward_report(self) -> RewardReport:
        """Read the verifier's verdict out of `/logs/verifier`."""
        return read_reward(
            lambda name: self.read_text(posixpath.join(self.paths.logs_verifier, name))
        )

    # --- docker helpers -----------------------------------------------------

    def _connect(self):
        if self._client is None:
            try:
                import docker
            except ImportError as exc:
                raise SandboxError(
                    "the OpenEnv runtime needs the Docker SDK: pip install 'repo2rlenv[openenv]'"
                ) from exc
            try:
                self._client = docker.from_env()
                self._client.ping()
            except Exception as exc:
                raise SandboxError(f"could not reach the Docker daemon: {exc}") from exc
        return self._client

    def _resolve_image(self, task: Repo2RLEnvTask, client) -> str:
        """Pull the pushed image when there is one, else build the task's Dockerfile."""
        pullable = task.pullable_image
        if pullable:
            try:
                client.images.get(pullable)
            except Exception:
                logger.info("pulling %s", pullable)
                client.images.pull(pullable)
            return pullable

        if task.dockerfile is None:
            raise SandboxError(
                f"task {task.task_id!r} has no environment/Dockerfile and no pushed image, so it "
                "cannot be executed. Text-only `pr_diff` tasks are scored against "
                "solution/patch.diff instead — see repo2rlenv.reward."
            )
        tag = f"repo2rlenv/{_slug(task.task_id)}:latest"
        logger.info("building %s from %s", tag, task.dockerfile)
        client.images.build(
            path=str(task.environment_dir), dockerfile=task.dockerfile.name, tag=tag, rm=True
        )
        return tag

    def _image_workdir(self, client) -> str:
        try:
            config = client.images.get(self._image).attrs.get("Config", {})
            return str(config.get("WorkingDir") or DEFAULT_WORKDIR)
        except Exception:  # pragma: no cover - image metadata is best-effort
            return DEFAULT_WORKDIR

    def _with_timeout(self, command: str, timeout_s: float) -> str:
        """Bound `command` with GNU timeout when the image provides it."""
        if self._has_timeout is None:
            container = self._require_container()
            probe, _ = container.exec_run(cmd=["sh", "-c", "command -v timeout"])
            self._has_timeout = int(probe) == 0
        if not self._has_timeout:
            return command
        return f"timeout -k 5 {int(max(timeout_s, 1))} bash -c {_quote(command)}"

    def _require_container(self):
        if self._container is None:
            raise SandboxError("sandbox is not running; call start() first")
        return self._container


def _container_limits(task: Repo2RLEnvTask) -> dict[str, object]:
    """Translate a task's declared policies into `containers.run` kwargs.

    Raises:
        SandboxError: if the task asks for something this backend cannot enforce
            faithfully. Failing closed matters more than breadth: silently
            granting a `no-network` task the daemon's default bridge would both
            corrupt the result and hand a sandboxed process the internet.
    """
    kwargs: dict[str, object] = {}

    if task.network_mode == NETWORK_ALLOWLIST:
        raise SandboxError(
            f"task {task.task_id!r} declares network_mode='allowlist', which needs a "
            "filtering proxy this backend does not run. Grade it with `harbor run`."
        )
    if task.network_mode == NETWORK_NONE:
        kwargs["network_mode"] = "none"

    if task.cpus is not None:
        # Docker expresses CPU count as a quota against a 100ms period.
        kwargs["cpu_period"] = 100_000
        kwargs["cpu_quota"] = int(task.cpus * 100_000)
    if task.memory_mb is not None:
        kwargs["mem_limit"] = f"{task.memory_mb}m"
    if task.gpus is not None:
        raise SandboxError(
            f"task {task.task_id!r} requests {task.gpus} GPU(s); this backend does not "
            "allocate accelerators. Grade it with `harbor run`."
        )
    return kwargs


def resolve_within(base: str, relative: str) -> str:
    """Resolve `relative` under `base`, rejecting anything that escapes it.

    Applied to every agent-supplied path so a policy cannot read or write
    outside the checkout — in particular it cannot reach /tests or /solution.

    Raises:
        ValueError: if `relative` is empty, is absolute, names the checkout
            itself, or escapes `base`.
    """
    if not relative.strip():
        raise ValueError("path must name a file relative to the working directory")
    if posixpath.isabs(relative):
        raise ValueError(f"path must be relative to the working directory: {relative!r}")
    base_norm = posixpath.normpath(base)
    target = posixpath.normpath(posixpath.join(base_norm, relative))
    if target == base_norm:
        # "", "." and "a/.." all land here. A read would try to cat a
        # directory; a write is worse — the target splits into ("/",
        # "workspace"), dropping a regular file over the whole checkout.
        raise ValueError(
            f"path must name a file inside the working directory, not the "
            f"directory itself: {relative!r}"
        )
    if not target.startswith(base_norm.rstrip("/") + "/"):
        raise ValueError(f"path escapes the working directory: {relative!r}")
    return target


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-") or "task"
