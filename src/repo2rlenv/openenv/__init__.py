"""Serve Repo2RLEnv datasets as OpenEnv environments.

[OpenEnv](https://github.com/huggingface/OpenEnv) is a Gymnasium-style standard
for containerized agentic environments — `reset()` / `step()` / `state` over a
WebSocket, deployable as a Docker image or a Hugging Face Space.

This subpackage is the second serving target alongside Harbor: the tasks are the
same directories `repo2rlenv generate` already writes, and this runtime serves
them as an episode loop a trainer can drive.

    repo2rlenv generate --repo pallets/click --pipeline pr_runtime --out ./tasks
    repo2rlenv export --format openenv ./tasks --out ./click-env
    docker build -t click-env ./click-env
    docker run --rm -p 8000:8000 -v /var/run/docker.sock:/var/run/docker.sock click-env

The serving pieces need the optional extra:

    pip install 'repo2rlenv[openenv]'

Because a task's starting state lives inside the image it carries, the runtime
is Docker-backed. When a dataset has been published with `repo2rlenv push`,
`[metadata.repo2env.reproducibility]` names an already-pushed image and the
runtime pulls it instead of rebuilding.

Serving names resolve lazily, so reading a dataset (`TaskSet`,
`Repo2RLEnvTask`) works with a plain `pip install repo2rlenv`; only the names
that actually serve traffic pull in `openenv`.
"""

from __future__ import annotations

from typing import Any

from repo2rlenv.openenv.dataset import Repo2RLEnvTask, TaskFormatError, TaskSet
from repo2rlenv.openenv.reward import RewardReport, read_reward

#: Attribute name -> submodule providing it. Everything listed here imports
#: `openenv`, so it is resolved on first access rather than at import time.
_LAZY: dict[str, str] = {
    "AGENT_ACTIONS": "models",
    "CONTROL_ACTIONS": "models",
    "DockerSandbox": "sandbox",
    "Repo2RLEnvAction": "models",
    "Repo2RLEnvClient": "client",
    "Repo2RLEnvEnvironment": "environment",
    "Repo2RLEnvObservation": "models",
    "Repo2RLEnvState": "models",
    "SandboxError": "sandbox",
    "build_app": "app",
}

__all__ = [
    "AGENT_ACTIONS",
    "CONTROL_ACTIONS",
    "DockerSandbox",
    "Repo2RLEnvAction",
    "Repo2RLEnvClient",
    "Repo2RLEnvEnvironment",
    "Repo2RLEnvObservation",
    "Repo2RLEnvState",
    "Repo2RLEnvTask",
    "RewardReport",
    "SandboxError",
    "TaskFormatError",
    "TaskSet",
    "build_app",
    "read_reward",
]


def __getattr__(name: str) -> Any:
    """Import the serving names on demand (PEP 562).

    Keeps `repo2rlenv.openenv.dataset` usable without the `openenv` extra
    installed, while `from repo2rlenv.openenv import Repo2RLEnvClient` still
    works for anyone who has it.
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    try:
        module = import_module(f"{__name__}.{module_name}")
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ImportError(
            f"{name} needs the OpenEnv runtime: pip install 'repo2rlenv[openenv]' ({exc})"
        ) from exc
    value = getattr(module, name)
    globals()[name] = value  # cache so __getattr__ runs once per name
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
