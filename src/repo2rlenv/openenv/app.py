"""FastAPI application factory for a Repo2RLEnv OpenEnv environment.

An emitted environment package's `server/app.py` is a two-liner over this:

    from repo2rlenv.openenv import build_app
    app = build_app()

Configuration is entirely by environment variable, so the same image serves any
dataset:

    REPO2RLENV_TASKS_DIR         directory of emitted tasks (default: ./tasks)
    REPO2RLENV_DEFAULT_TASK_ID   task used when reset() names none
    REPO2RLENV_COMMAND_TIMEOUT_S timeout for agent exec actions (default: 120)
    MAX_CONCURRENT_ENVS          concurrent WebSocket sessions (default: 8)
"""

from __future__ import annotations

import os
from typing import Any

from openenv.core.env_server.http_server import create_app

from repo2rlenv.openenv.environment import DEFAULT_COMMAND_TIMEOUT_S, Repo2RLEnvEnvironment
from repo2rlenv.openenv.models import Repo2RLEnvAction, Repo2RLEnvObservation


def build_app(**kwargs: Any):
    """Build the FastAPI app serving the configured dataset.

    Args:
        **kwargs: forwarded to `openenv.core.env_server.http_server.create_app`.

    Returns:
        The FastAPI application.
    """

    def _factory() -> Repo2RLEnvEnvironment:
        """Called once per session by the server."""
        return Repo2RLEnvEnvironment(
            command_timeout_s=float(
                os.getenv("REPO2RLENV_COMMAND_TIMEOUT_S", DEFAULT_COMMAND_TIMEOUT_S)
            ),
        )

    kwargs.setdefault("env_name", "repo2rlenv")
    kwargs.setdefault("max_concurrent_envs", int(os.getenv("MAX_CONCURRENT_ENVS", "8")))
    return create_app(_factory, Repo2RLEnvAction, Repo2RLEnvObservation, **kwargs)


def main(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the server directly: `python -m repo2rlenv.openenv.app`."""
    import uvicorn

    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serve a Repo2RLEnv dataset over OpenEnv")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    main(host=args.host, port=args.port)
