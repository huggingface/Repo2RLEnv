"""Serve a Repo2RLEnv dataset as an OpenEnv environment.

`repo2rlenv generate` produces verifiable task directories; this turns a
directory of them into a Gymnasium-style environment a trainer can drive over
OpenEnv's `reset()` / `step()` / `state` API.

The module is deliberately thin. It decides which task to run
(`dataset.TaskSet`), asks the sandbox to execute things (`sandbox.DockerSandbox`),
and forwards the verifier's verdict (`reward.RewardReport`). It never computes a
reward itself — the reward is the one our own `tests/test.sh` wrote, unchanged.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from openenv.core.env_server.interfaces import Environment

from repo2rlenv.openenv.dataset import Repo2RLEnvTask, TaskSet
from repo2rlenv.openenv.models import (
    CONTROL_ACTIONS,
    Repo2RLEnvAction,
    Repo2RLEnvObservation,
    Repo2RLEnvState,
)
from repo2rlenv.openenv.sandbox import DockerSandbox, ExecResult, SandboxError, resolve_within

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_TIMEOUT_S = 120.0

#: Upper bound on how many ids `Repo2RLEnvState.available_tasks` carries, so a
#: dataset with thousands of tasks does not bloat every state response.
MAX_LISTED_TASKS = 200

_Handler = Callable[[Repo2RLEnvAction, DockerSandbox, Repo2RLEnvTask], Repo2RLEnvObservation]


class Repo2RLEnvEnvironment(Environment[Repo2RLEnvAction, Repo2RLEnvObservation, Repo2RLEnvState]):
    """Runs one Repo2RLEnv task per episode.

    Each `reset()` boots a fresh container for one task. The agent then works
    through `exec` / `read` / `write`, and the training loop ends the episode
    with `evaluate`, which runs the task's own verifier and forwards whatever
    reward it wrote.

    Args:
        dataset: directory of emitted tasks. Defaults to `$REPO2RLENV_TASKS_DIR`,
            then to the `tasks/` directory next to this environment.
        default_task_id: task used when `reset()` names none. Defaults to
            `$REPO2RLENV_DEFAULT_TASK_ID`.
        command_timeout_s: timeout applied to agent `exec` actions. The verifier
            and the oracle instead use the timeouts declared in `task.toml`.
        sandbox_factory: builds the sandbox. Injected by tests.
    """

    # Every episode gets its own container, so sessions share no mutable state.
    SUPPORTS_CONCURRENT_SESSIONS: bool = True

    def __init__(
        self,
        dataset: str | None = None,
        default_task_id: str | None = None,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        allow_control_actions: bool | None = None,
        sandbox_factory: Callable[[], DockerSandbox] | None = None,
    ) -> None:
        super().__init__()
        self.command_timeout_s = command_timeout_s
        self.allow_control_actions = (
            allow_control_actions
            if allow_control_actions is not None
            else (
                os.getenv("REPO2RLENV_ALLOW_CONTROL_ACTIONS", "1").strip().lower()
                not in {"0", "false", "no", "off"}
            )
        )
        self.default_task_id = default_task_id or os.getenv("REPO2RLENV_DEFAULT_TASK_ID") or None
        self._sandbox_factory = sandbox_factory or DockerSandbox
        self.tasks = TaskSet(_resolve_dataset(dataset))

        self._task: Repo2RLEnvTask | None = None
        self._sandbox: DockerSandbox | None = None
        self._state = Repo2RLEnvState()
        self._refresh_dataset_state()

        self._handlers: dict[str, _Handler] = {
            "exec": self._do_exec,
            "read": self._do_read,
            "write": self._do_write,
            "evaluate": self._do_evaluate,
            "solve": self._do_solve,
        }

    # --- Gymnasium API ------------------------------------------------------

    def reset(
        self,
        seed: int | None = None,
        episode_id: str | None = None,
        **kwargs: Any,
    ) -> Repo2RLEnvObservation:
        """Start a fresh episode on one task.

        Accepts `task_id` to choose the task; defaults to `default_task_id`, or
        to the only task in the dataset when there is exactly one.
        """
        del seed

        self.close()
        # A failed reset must not leave the previous episode in state: close()
        # has already torn the container down, so anything read from `state`
        # after a raise below would describe a task that is not running.
        self._state = Repo2RLEnvState()
        self._refresh_dataset_state()

        task = self.tasks.get(self._select_task_id(kwargs.get("task_id")))
        sandbox = self._sandbox_factory()
        try:
            sandbox.start(task)
        except Exception:
            sandbox.close()
            raise

        self._task = task
        self._sandbox = sandbox
        self._state = Repo2RLEnvState(
            episode_id=episode_id or str(uuid4()),
            task_id=task.task_id,
            task_name=task.name,
            task_path=str(task.path),
            pipeline=task.pipeline,
            image=sandbox.image,
            workdir=sandbox.paths.workdir,
        )
        self._refresh_dataset_state()

        return self._observe(
            "reset",
            output="",
            info={
                "task": task.summary(),
                # Agent-visible paths only — the observation reaches the policy.
                "paths": sandbox.paths.as_env(agent_visible=True),
            },
        )

    def step(
        self,
        action: Repo2RLEnvAction,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> Repo2RLEnvObservation:
        """Apply one action to the running episode."""
        del timeout_s, kwargs

        if not isinstance(action, Repo2RLEnvAction):
            raise TypeError(f"expected Repo2RLEnvAction, got {type(action).__name__}")

        self._state.step_count += 1
        self._state.last_action_type = action.action_type

        try:
            if self._state.evaluated:
                # `evaluate` ends the episode. Serving further actions would let
                # a caller step past a terminal state and, worse, report
                # done=False after already reporting done=True.
                raise SandboxError(
                    "the episode ended when the verifier ran; call reset() to start another"
                )
            if action.action_type in CONTROL_ACTIONS and not self.allow_control_actions:
                raise PermissionError(
                    f"{action.action_type!r} is a training-orchestration control, not an "
                    "agent action; the server was started with "
                    "REPO2RLENV_ALLOW_CONTROL_ACTIONS=0"
                )
            sandbox, task = self._require_episode()
            return self._handlers[action.action_type](action, sandbox, task)
        except Exception as exc:
            # Invalid actions and sandbox failures come back in the observation
            # so a policy can recover; only server faults propagate.
            logger.info("action %s failed: %s", action.action_type, exc)
            return self._observe(
                action.action_type,
                output="",
                success=False,
                error=str(exc),
                # A terminated episode stays terminated — never walk done back.
                done=self._state.evaluated,
            )

    @property
    def state(self) -> Repo2RLEnvState:
        """Current episode state."""
        return self._state

    def close(self) -> None:
        """Tear down the running sandbox, if any."""
        if self._sandbox is not None:
            self._sandbox.close()
        self._sandbox = None
        self._task = None

    # --- action handlers ----------------------------------------------------

    def _do_exec(
        self, action: Repo2RLEnvAction, sandbox: DockerSandbox, task: Repo2RLEnvTask
    ) -> Repo2RLEnvObservation:
        del task
        if not action.command.strip():
            raise ValueError("exec requires a non-empty command")
        result = sandbox.exec(
            action.command,
            timeout_s=action.timeout_s or self.command_timeout_s,
            user=sandbox.agent_user,
            agent_visible=True,
        )
        return self._from_exec("exec", result)

    def _do_read(
        self, action: Repo2RLEnvAction, sandbox: DockerSandbox, task: Repo2RLEnvTask
    ) -> Repo2RLEnvObservation:
        del task
        target = resolve_within(sandbox.paths.workdir, action.path)
        content = sandbox.read_text(target)
        if content is None:
            raise FileNotFoundError(f"no such file: {action.path}")
        return self._observe("read", output=content)

    def _do_write(
        self, action: Repo2RLEnvAction, sandbox: DockerSandbox, task: Repo2RLEnvTask
    ) -> Repo2RLEnvObservation:
        del task
        target = resolve_within(sandbox.paths.workdir, action.path)
        sandbox.write_text(target, action.content)
        return self._observe("write", output=f"wrote {len(action.content)} bytes to {action.path}")

    def _do_evaluate(
        self, action: Repo2RLEnvAction, sandbox: DockerSandbox, task: Repo2RLEnvTask
    ) -> Repo2RLEnvObservation:
        """Run the task's verifier and forward its reward. Ends the episode."""
        del action
        result = sandbox.run_verifier(task)
        report = sandbox.reward_report()

        self._state.evaluated = True
        self._state.reward = report.value
        self._state.last_exit_code = result.exit_code

        info = {**report.as_info(), "verifier_exit_code": result.exit_code}
        if not report.graded:
            # No reward file means the episode cannot be scored. Say so rather
            # than inventing a number indistinguishable from a genuine 0.0.
            return self._observe(
                "evaluate",
                output=result.output,
                success=False,
                error=(
                    f"the verifier wrote no reward file to {sandbox.paths.logs_verifier}; "
                    "the episode cannot be scored"
                ),
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                done=True,
                info=info,
            )
        return self._observe(
            "evaluate",
            output=result.output,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            reward=report.value,
            done=True,
            info=info,
        )

    def _do_solve(
        self, action: Repo2RLEnvAction, sandbox: DockerSandbox, task: Repo2RLEnvTask
    ) -> Repo2RLEnvObservation:
        """Apply the task's own oracle patch.

        Orchestration-only: it proves a task is solvable and produces gold
        trajectories. Follow it with `evaluate`, which should return full reward.
        """
        del action
        return self._from_exec("solve", sandbox.run_solution(task))

    # --- observation plumbing -----------------------------------------------

    def _from_exec(self, action_type: str, result: ExecResult) -> Repo2RLEnvObservation:
        self._state.last_exit_code = result.exit_code
        return self._observe(
            action_type,
            output=result.output,
            success=result.ok,
            error="command timed out" if result.timed_out else "",
            exit_code=result.exit_code,
            timed_out=result.timed_out,
        )

    def _observe(
        self,
        action_type: str,
        output: str,
        success: bool = True,
        error: str = "",
        exit_code: int | None = None,
        timed_out: bool = False,
        reward: float | None = None,
        done: bool = False,
        info: dict[str, Any] | None = None,
    ) -> Repo2RLEnvObservation:
        return Repo2RLEnvObservation(
            instruction=self._task.instruction if self._task else "",
            output=output,
            action_type=action_type,
            success=success,
            error=error,
            exit_code=exit_code,
            timed_out=timed_out,
            task_id=self._state.task_id,
            task_name=self._state.task_name,
            pipeline=self._state.pipeline,
            workdir=self._state.workdir,
            info=info or {},
            reward=reward,
            done=done,
        )

    # --- helpers ------------------------------------------------------------

    def _select_task_id(self, requested: str | None) -> str:
        task_id = requested or self.default_task_id
        if task_id:
            return task_id
        available = self.tasks.task_ids()
        if len(available) == 1:
            return available[0]
        raise ValueError(
            f"reset() needs a task_id: the dataset at {self.tasks.root} holds "
            f"{len(available)} tasks. Available: {available[:10]}"
            f"{' ...' if len(available) > 10 else ''}"
        )

    def _require_episode(self) -> tuple[DockerSandbox, Repo2RLEnvTask]:
        if self._sandbox is None or self._task is None:
            raise SandboxError("no episode is running; call reset() first")
        return self._sandbox, self._task

    def _refresh_dataset_state(self) -> None:
        task_ids = self.tasks.task_ids()
        self._state.task_count = len(task_ids)
        self._state.available_tasks = task_ids[:MAX_LISTED_TASKS]


def _resolve_dataset(dataset: str | None) -> str:
    """Where to look for tasks: the argument, the env var, then ./tasks."""
    return dataset or os.getenv("REPO2RLENV_TASKS_DIR") or "tasks"
