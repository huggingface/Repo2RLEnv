"""Write a deployable OpenEnv environment around an emitted dataset.

Where `harbor.py` emits the *tasks*, this emits the *environment that serves
them*: a directory you can `docker build`, `docker run`, or push to a Hugging
Face Space, which then speaks OpenEnv's `reset()` / `step()` / `state` API.

    <dest>/
      openenv.yaml       OpenEnv manifest
      Dockerfile         builds the server image
      README.md          Space card (front-matter + usage)
      pyproject.toml     declares the repo2rlenv[openenv] dependency
      server/app.py      two lines over repo2rlenv.openenv.build_app
      tasks/<id>/...     the emitted task directories, copied verbatim

The tasks are copied *unchanged* — this adds a serving layer, it does not
convert the data. The runtime lives in `repo2rlenv.openenv`, so the emitted
package stays thin config rather than generated logic.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The environment layout and manifest schema (`openenv.yaml`, the FastAPI +
WebSocket server contract, the Space packaging) are defined by:

  OpenEnv (Meta PyTorch + Hugging Face)
  https://github.com/huggingface/OpenEnv    (BSD-3-Clause)

We target that format directly so our datasets are servable by any
OpenEnv-compatible trainer. No OpenEnv code is copied; the emitted package
depends on the published `openenv` distribution.

Released under Apache-2.0.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from repo2rlenv import __version__
from repo2rlenv.openenv.dataset import TaskSet

#: Port the emitted server listens on. Hugging Face Spaces routes to this.
DEFAULT_PORT = 8000

#: Base image for the emitted server. Slim + Docker CLI is all it needs; the
#: task containers run on the host daemon, not inside this image.
DEFAULT_BASE_IMAGE = "python:3.12-slim"


class EmitError(ValueError):
    """Raised when a dataset cannot be turned into an environment."""


@dataclass(frozen=True)
class OpenEnvPackage:
    """What `write_openenv_env` produced.

    Attributes:
        path: the emitted environment directory.
        name: the environment name written into `openenv.yaml`.
        task_count: how many task directories were bundled.
        runnable_task_count: how many of those can actually be executed.
    """

    path: Path
    name: str
    task_count: int
    runnable_task_count: int


def write_openenv_env(
    dataset_dir: str | Path,
    dest_dir: str | Path,
    *,
    name: str | None = None,
    description: str | None = None,
    requirement: str | None = None,
    base_image: str = DEFAULT_BASE_IMAGE,
    port: int = DEFAULT_PORT,
) -> OpenEnvPackage:
    """Emit an OpenEnv environment serving the tasks in `dataset_dir`.

    Args:
        dataset_dir: a directory of emitted tasks (the output of `generate`).
        dest_dir: where to write the environment package.
        name: environment name; defaults to the dataset directory name.
        description: one-line description for the manifest and Space card.
        requirement: the repo2rlenv requirement the image installs. Defaults to
            `repo2rlenv[openenv]>=<installed version>`; override to pin a
            release, a git ref, or a local wheel.
        base_image: base image for the server.
        port: port the server listens on.

    Returns:
        OpenEnvPackage describing what was written.

    Raises:
        EmitError: if `dataset_dir` holds no tasks.
    """
    source = Path(dataset_dir).expanduser().resolve()
    dest = Path(dest_dir).expanduser().resolve()

    task_set = TaskSet(source)
    task_ids = task_set.task_ids()
    if not task_ids:
        raise EmitError(f"no task.toml files found under {source}; is this a generated dataset?")

    tasks = [task_set.get(tid) for tid in task_ids]
    runnable = [t for t in tasks if t.runnable]
    env_name = _slug(name or source.name)
    summary = description or _describe(tasks)
    requirement = requirement or f"repo2rlenv[openenv]>={__version__}"

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "server").mkdir(exist_ok=True)

    _copy_tasks(tasks, dest / "tasks")
    (dest / "openenv.yaml").write_text(_manifest(env_name, summary, port), encoding="utf-8")
    (dest / "Dockerfile").write_text(
        _dockerfile(env_name, base_image, requirement, port), encoding="utf-8"
    )
    (dest / "pyproject.toml").write_text(
        _pyproject(env_name, summary, requirement), encoding="utf-8"
    )
    (dest / "README.md").write_text(
        _readme(env_name, summary, tasks, runnable, port), encoding="utf-8"
    )
    (dest / "server" / "__init__.py").write_text(
        '"""Server package for the emitted OpenEnv environment."""\n', encoding="utf-8"
    )
    (dest / "server" / "app.py").write_text(_app_module(), encoding="utf-8")
    (dest / ".dockerignore").write_text("__pycache__/\n*.pyc\n.venv/\n", encoding="utf-8")

    return OpenEnvPackage(
        path=dest,
        name=env_name,
        task_count=len(tasks),
        runnable_task_count=len(runnable),
    )


def _copy_tasks(tasks, tasks_root: Path) -> None:
    """Copy each task directory under `tasks/<task-id>/`, replacing any old copy."""
    if tasks_root.exists():
        shutil.rmtree(tasks_root)
    tasks_root.mkdir(parents=True)
    for task in tasks:
        shutil.copytree(task.path, tasks_root / task.task_id, dirs_exist_ok=True)


def _manifest(name: str, description: str, port: int) -> str:
    """The `openenv.yaml` manifest OpenEnv tooling reads."""
    payload = {
        "spec_version": 1,
        "name": name,
        "version": "0.1.0",
        "description": description,
        "type": "space",
        "runtime": "fastapi",
        "app": "server.app:app",
        "port": port,
        "action": "Repo2RLEnvAction",
        "observation": "Repo2RLEnvObservation",
    }
    return yaml.safe_dump(payload, sort_keys=False)


def _app_module() -> str:
    return '''"""FastAPI entry point for this environment.

The runtime lives in `repo2rlenv.openenv`; this module only names it so the
image has a stable import path (`server.app:app`).
"""

from repo2rlenv.openenv import build_app

app = build_app()
'''


def _dockerfile(name: str, base_image: str, requirement: str, port: int) -> str:
    return f"""# Serves this dataset over OpenEnv's WebSocket API.
#
# Task containers run on the HOST Docker daemon, so the socket must be mounted:
#
#   docker build -t {name} .
#   docker run --rm -p {port}:{port} -v /var/run/docker.sock:/var/run/docker.sock {name}
#
FROM {base_image}

# git backs `git apply` in the oracle path; the docker CLI is handy for
# debugging the task containers this server starts.
RUN apt-get update \\
    && apt-get install -y --no-install-recommends ca-certificates curl git \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir "{requirement}" "uvicorn[standard]"

COPY server/ /app/server/
COPY tasks/ /app/tasks/

ENV REPO2RLENV_TASKS_DIR=/app/tasks
ENV PYTHONUNBUFFERED=1

EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \\
    CMD curl -f http://localhost:{port}/health || exit 1

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "{port}"]
"""


def _pyproject(name: str, description: str, requirement: str) -> str:
    return f"""[build-system]
requires = ["setuptools>=61", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}"
version = "0.1.0"
description = "{description}"
requires-python = ">=3.10"
dependencies = [
    "{requirement}",
    "uvicorn[standard]>=0.30.0",
]

[tool.setuptools]
packages = ["server"]
"""


def _readme(name: str, description: str, tasks, runnable, port: int) -> str:
    """Hugging Face Space card: YAML front-matter plus usage."""
    pipelines = sorted({t.pipeline for t in tasks if t.pipeline})
    unrunnable = len(tasks) - len(runnable)
    lines = [
        "---",
        f"title: {name}",
        'emoji: "🧪"',
        "colorFrom: indigo",
        "colorTo: purple",
        "sdk: docker",
        f"app_port: {port}",
        "pinned: false",
        "tags:",
        "  - openenv",
        "  - repo2rlenv",
        "  - rl",
        "---",
        "",
        f"# {name}",
        "",
        description,
        "",
        "Generated by [Repo2RLEnv](https://github.com/huggingface/Repo2RLEnv) with",
        "`repo2rlenv export --format openenv`. The tasks under `tasks/` are the",
        "unmodified [Harbor](https://www.harborframework.com/docs/tasks) task",
        "directories `repo2rlenv generate` emitted, so the same dataset also runs",
        "under `harbor run`.",
        "",
        "## Contents",
        "",
        f"- **{len(tasks)}** task(s)" + (f", **{len(runnable)}** runnable" if unrunnable else ""),
    ]
    if pipelines:
        lines.append(f"- Pipeline(s): {', '.join(pipelines)}")
    if unrunnable:
        lines.append(
            f"- {unrunnable} text-only task(s) carry no image or verifier and are scored "
            "against `solution/patch.diff` client-side rather than executed."
        )
    lines += [
        "",
        "## Run it",
        "",
        "```bash",
        f"docker build -t {name} .",
        "# task containers run on the host daemon, so mount the socket",
        f"docker run --rm -p {port}:{port} \\",
        "    -v /var/run/docker.sock:/var/run/docker.sock \\",
        f"    {name}",
        "```",
        "",
        "```python",
        "import asyncio",
        "from repo2rlenv.openenv import Repo2RLEnvClient",
        "",
        "async def main():",
        f'    env = Repo2RLEnvClient(base_url="http://localhost:{port}")',
        f'    start = await env.reset(task_id="{tasks[0].task_id}")',
        "    print(start.observation.instruction)",
        "",
        '    await env.run("pytest -q")            # the agent works',
        "    result = await env.evaluate()          # the task's own verifier grades it",
        "    print(result.reward)",
        "    await env.close()",
        "",
        "asyncio.run(main())",
        "```",
        "",
        "## Rewards",
        "",
        "The reward is produced by each task's own `tests/test.sh` and only",
        "forwarded — never recomputed here. A verifier that writes no reward file",
        "yields `reward=None` and an explicit error rather than a fabricated `0.0`.",
        "The diagnostic breakdown (F2P/P2P counts, `resolved`, `parse_status`)",
        'arrives as `observation.info["reward_details"]`.',
        "",
        "## Configuration",
        "",
        "| Variable | Default | Meaning |",
        "|---|---|---|",
        "| `REPO2RLENV_TASKS_DIR` | `/app/tasks` | Where the tasks live |",
        "| `REPO2RLENV_DEFAULT_TASK_ID` | — | Task used when `reset()` names none |",
        "| `REPO2RLENV_COMMAND_TIMEOUT_S` | `120` | Timeout for agent `exec` actions |",
        "| `MAX_CONCURRENT_ENVS` | `8` | Concurrent WebSocket sessions |",
        "",
    ]
    return "\n".join(lines)


def _describe(tasks) -> str:
    pipelines = sorted({t.pipeline for t in tasks if t.pipeline})
    suffix = f" ({', '.join(pipelines)})" if pipelines else ""
    return f"{len(tasks)} verifiable Repo2RLEnv task(s) served as an OpenEnv environment{suffix}"


def _slug(value: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in value.lower())
    return cleaned.strip("-") or "repo2rlenv-env"
