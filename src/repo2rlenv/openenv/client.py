"""Client for a Repo2RLEnv OpenEnv environment.

This is what a trainer imports. It never imports anything from the server side.
"""

from __future__ import annotations

from typing import Any

from openenv.core.client_types import StepResult
from openenv.core.env_client import EnvClient

from repo2rlenv.openenv.models import (
    Repo2RLEnvAction,
    Repo2RLEnvObservation,
    Repo2RLEnvState,
)


class Repo2RLEnvClient(EnvClient[Repo2RLEnvAction, Repo2RLEnvObservation, Repo2RLEnvState]):
    """Drives a Repo2RLEnv task server over OpenEnv's WebSocket API.

    `run` / `read_file` / `write_file` are what an agent does; `evaluate` grades
    the episode and ends it, and `solve` applies the task's oracle patch.

    Example:

        import asyncio
        from repo2rlenv.openenv import Repo2RLEnvClient

        async def main():
            env = Repo2RLEnvClient(base_url="http://localhost:8000")
            start = await env.reset(task_id="pallets__click-2951")
            print(start.observation.instruction)

            await env.write_file("src/click/core.py", patched)
            result = await env.evaluate()
            print(result.reward, result.observation.info["reward_details"])
            await env.close()

        asyncio.run(main())
    """

    async def run(
        self, command: str, timeout_s: float | None = None
    ) -> StepResult[Repo2RLEnvObservation]:
        """Run a shell command in the task's working directory."""
        return await self.step(
            Repo2RLEnvAction(action_type="exec", command=command, timeout_s=timeout_s)
        )

    async def read_file(self, path: str) -> StepResult[Repo2RLEnvObservation]:
        """Read a file, relative to the task's working directory."""
        return await self.step(Repo2RLEnvAction(action_type="read", path=path))

    async def write_file(self, path: str, content: str) -> StepResult[Repo2RLEnvObservation]:
        """Write a file, relative to the task's working directory."""
        return await self.step(Repo2RLEnvAction(action_type="write", path=path, content=content))

    async def evaluate(self) -> StepResult[Repo2RLEnvObservation]:
        """Run the task's verifier and end the episode.

        The reward is whatever `tests/test.sh` wrote; the per-metric breakdown
        is in `observation.info`.
        """
        return await self.step(Repo2RLEnvAction(action_type="evaluate"))

    async def solve(self) -> StepResult[Repo2RLEnvObservation]:
        """Apply the task's oracle patch.

        Orchestration tooling for validating a dataset, not part of an agent's
        action space. Follow with `evaluate` to confirm full reward.
        """
        return await self.step(Repo2RLEnvAction(action_type="solve"))

    # --- wire protocol ------------------------------------------------------

    def _step_payload(self, action: Repo2RLEnvAction) -> dict[str, Any]:
        return action.model_dump()

    def _parse_result(self, payload: dict[str, Any]) -> StepResult[Repo2RLEnvObservation]:
        data = dict(payload.get("observation", {}))
        data["reward"] = payload.get("reward")
        data["done"] = payload.get("done", False)
        observation = Repo2RLEnvObservation.model_validate(data)
        return StepResult(
            observation=observation,
            reward=observation.reward,
            done=observation.done,
        )

    def _parse_state(self, payload: dict[str, Any]) -> Repo2RLEnvState:
        return Repo2RLEnvState.model_validate(payload)
