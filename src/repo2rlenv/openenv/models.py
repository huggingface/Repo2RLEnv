"""OpenEnv wire types for a Repo2RLEnv task environment.

Shared by the client and the server; neither side imports the other.

`openenv` is an optional dependency — install it with
`pip install 'repo2rlenv[openenv]'`.
"""

from __future__ import annotations

from typing import Any, Literal

from openenv.core.env_server.types import Action, Observation, State
from pydantic import Field

#: What an agent may do while solving the task.
AGENT_ACTIONS: frozenset[str] = frozenset({"exec", "read", "write"})

#: Reserved for training orchestration. `evaluate` grades the episode and ends
#: it; `solve` applies the task's own oracle patch. Neither belongs to the
#: agent's action space — an agent that could grade or solve on demand would be
#: able to end its own episode or read out the answer.
CONTROL_ACTIONS: frozenset[str] = frozenset({"evaluate", "solve"})

ActionType = Literal["exec", "read", "write", "evaluate", "solve"]


class Repo2RLEnvAction(Action):
    """One interaction with a Repo2RLEnv task sandbox.

    Attributes:
        action_type: exec / read / write (agent), or evaluate / solve (orchestration).
        command: shell command, for `exec`.
        path: path relative to the working directory, for `read` and `write`.
        content: file contents, for `write`.
        timeout_s: overrides the default command timeout; ignored by
            `evaluate` and `solve`, which use the timeouts in `task.toml`.
    """

    action_type: ActionType = Field(default="exec")
    command: str = Field(default="")
    path: str = Field(default="")
    content: str = Field(default="")
    timeout_s: float | None = Field(default=None, gt=0)


class Repo2RLEnvObservation(Observation):
    """What the sandbox reported back.

    Attributes:
        instruction: the task's instruction.md, repeated every step so a
            stateless policy can always see the goal.
        output: merged stdout/stderr, file contents for `read`, or a short
            confirmation for `write`.
        action_type: the action this observation answers, or "reset".
        success: whether the action itself completed. Unrelated to the reward —
            a failing test command is still a successful `exec`.
        error: why the action failed, when it did.
        exit_code: exit status, for actions that ran a command.
        timed_out: whether the command was killed by its timeout.
        task_id: identifier of the running task.
        task_name: the task's qualified `<org>/<slug>` name.
        pipeline: which synthesis pipeline produced the task.
        workdir: working directory inside the sandbox; `path` is relative to it.
        info: extra detail — the reward breakdown after `evaluate`, the task
            digest after `reset()`.
    """

    instruction: str = Field(default="")
    output: str = Field(default="")
    action_type: str = Field(default="")
    success: bool = Field(default=True)
    error: str = Field(default="")
    exit_code: int | None = Field(default=None)
    timed_out: bool = Field(default=False)
    task_id: str = Field(default="")
    task_name: str = Field(default="")
    pipeline: str = Field(default="")
    workdir: str = Field(default="")
    info: dict[str, Any] = Field(default_factory=dict)


class Repo2RLEnvState(State):
    """Server-side episode state, for training orchestration.

    Attributes:
        task_id: identifier of the running task, empty before the first reset().
        task_name: the task's qualified name.
        task_path: task directory on the server.
        pipeline: which synthesis pipeline produced the task.
        image: the image backing the sandbox.
        workdir: working directory inside the sandbox.
        available_tasks: task identifiers discovered in the dataset, truncated
            for large datasets — see `task_count` for the true total.
        task_count: number of tasks in the dataset.
        last_action_type: the most recent action.
        last_exit_code: exit status of the most recent command.
        evaluated: whether the verifier has run; the episode ends when it has.
        reward: reward reported by the verifier, once it has run.
    """

    task_id: str = Field(default="")
    task_name: str = Field(default="")
    task_path: str = Field(default="")
    pipeline: str = Field(default="")
    image: str = Field(default="")
    workdir: str = Field(default="")
    available_tasks: list[str] = Field(default_factory=list)
    task_count: int = Field(default=0)
    last_action_type: str = Field(default="")
    last_exit_code: int | None = Field(default=None)
    evaluated: bool = Field(default=False)
    reward: float | None = Field(default=None)
