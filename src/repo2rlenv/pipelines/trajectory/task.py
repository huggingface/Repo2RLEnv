"""The lifecycle every task family shares.

A family finds candidates in a trajectory and fills in a few hooks; `make_tasks` does the rest:

  find_candidates -> prepare -> build the environment image -> check_in_image
    -> write the task files -> run the trial matrix -> keep, or drop with a reason

Any hook can drop a candidate by raising `Reject("reason")`. A kept candidate is a Harbor task
folder (task.toml, instruction.md, environment/, tests/, solution/) that passed every trial:
its own environment image, tests/ at /tests, solution/ at /solution, networking off.
"""

from __future__ import annotations

import shlex
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from .docker import build_image, remove_as_root, run_in_container
from .llm import BudgetExceeded
from .trajectory import Trajectory

# Terminal agents (e.g. Harbor's terminus-2) drive a tmux session; tasks run offline, so it must be in the image.
AGENT_TOOLS = "RUN apt-get update && apt-get install -y --no-install-recommends tmux && rm -rf /var/lib/apt/lists/*"


class Reject(Exception):
    def __init__(self, reason: str, detail: object = None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


@dataclass
class Trial:
    name: str
    setup_script: str  # runs in the task image before tests/test.sh
    expected_reward: str  # first line of /logs/verifier/reward.txt, compared as text


class Family:
    """Base class for task families: override the hooks you need."""

    name = "family"

    def find_candidates(self, trajectory: Trajectory) -> list:
        raise NotImplementedError

    def task_id(self, candidate) -> str:
        raise NotImplementedError

    def prepare(self, candidate) -> None:
        """Cheap checks (and LLM calls) before any Docker work."""

    def environment_files(self, candidate) -> dict[str, str]:
        """Files of environment/ (at least a Dockerfile)."""
        raise NotImplementedError

    def image_tag(self, candidate) -> str:
        return f"repo2rlenv-trajectory-{self.name}-{self.task_id(candidate)}"

    def check_in_image(self, candidate, image: str, scratch_dir: Path) -> None:
        """Checks that need the built environment image."""

    def task_files(self, candidate) -> dict[str, str]:
        """Everything else: task.toml, instruction.md, tests/, solution/."""
        raise NotImplementedError

    def verifier_env(self, candidate) -> dict[str, str]:
        """Environment variables for tests/test.sh (task.toml [verifier.env])."""
        return {}

    def trials(self, candidate, scratch_dir: Path) -> tuple[list[Trial], dict[Path, str]]:
        """The trial matrix, plus any extra (host path -> container path) mounts it needs."""
        raise NotImplementedError

    def summary(self, candidate) -> dict:
        """Extra fields for the report and manifest."""
        return {}


def make_tasks(
    family: Family,
    trajectory: Trajectory,
    out_dir: Path,
    seen_task_ids: set[str],
    propose_only: bool = False,
) -> Iterator[dict]:
    """Yield one record per candidate; kept candidates are written to out_dir/<task_id>/.

    With propose_only, stop after `prepare`: this makes every LLM call (filling the cache) without Docker.
    """
    for candidate in family.find_candidates(trajectory):
        task_id = family.task_id(candidate)
        task_dir, scratch_dir = out_dir / task_id, out_dir / ".scratch" / task_id
        record = {"task_id": task_id, "family": family.name, "source_trace": trajectory.source}
        try:
            if task_id in seen_task_ids:
                raise Reject("duplicate")
            seen_task_ids.add(task_id)
            family.prepare(candidate)
            if propose_only:
                yield {**record, "kept": False, "reason": "proposed"}
                continue
            fresh_directory(task_dir)
            write_files(task_dir / "environment", family.environment_files(candidate))
            image = family.image_tag(candidate)
            if error := build_image(image, task_dir / "environment"):
                raise Reject("image_build_failed", error)
            fresh_directory(scratch_dir)
            family.check_in_image(candidate, image, scratch_dir)
            write_files(task_dir, family.task_files(candidate))
            trials, mounts = family.trials(candidate, scratch_dir)
            rewards = run_trials(task_dir, image, trials, mounts, family.verifier_env(candidate))
            if any(rewards[trial.name] != trial.expected_reward for trial in trials):
                raise Reject("trials_failed", rewards)
            yield {**record, "kept": True, "reason": "ok", **family.summary(candidate)}
        except Reject as rejection:
            shutil.rmtree(task_dir, ignore_errors=True)
            yield {**record, "kept": False, "reason": rejection.reason, "detail": rejection.detail}
        except (
            Exception
        ) as error:  # one bad candidate (e.g. an LLM call that kept failing) must not stop a batch
            shutil.rmtree(task_dir, ignore_errors=True)
            reason = "budget_exhausted" if isinstance(error, BudgetExceeded) else "error"
            yield {
                **record,
                "kept": False,
                "reason": reason,
                "detail": f"{type(error).__name__}: {error}"[:500],
            }
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)


def run_trials(
    task_dir: Path,
    image: str,
    trials: list[Trial],
    extra_mounts: dict[Path, str],
    verifier_env: dict[str, str],
) -> dict[str, str]:
    mounts = {
        task_dir / "tests": "/tests:ro",
        task_dir / "solution": "/solution:ro",
        **extra_mounts,
    }
    exports = "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in verifier_env.items()
    )
    rewards = {}
    for trial in trials:
        script = (
            f"{trial.setup_script}\n{exports}bash /tests/test.sh >/dev/null 2>&1\n"
            "echo; echo REWARD=$(head -n 1 /logs/verifier/reward.txt)"
        )
        _, output = run_in_container(image, script, mounts)
        rewards[trial.name] = next(
            (
                line[len("REWARD=") :]
                for line in reversed(output.splitlines())
                if line.startswith("REWARD=")
            ),
            "no reward",
        )
    return rewards


def task_toml(
    family: str,
    task_id: str,
    description: str,
    metadata: dict,
    *,
    verifier_env: dict | None = None,
    allow_internet: bool = False,
    cpus: int = 1,
    memory_mb: int = 2048,
    agent_timeout_sec: float = 1200.0,
    healthcheck_command: str | None = None,
) -> str:
    environment = {
        "build_timeout_sec": 1200.0,
        "os": "linux",
        "cpus": cpus,
        "memory_mb": memory_mb,
        "storage_mb": 10240,
        "gpus": 0,
        "allow_internet": allow_internet,
        "mcp_servers": [],
    }
    if healthcheck_command:  # Harbor runs it after container start, before the agent
        environment["healthcheck"] = {
            "command": healthcheck_command,
            "interval_sec": 2.0,
            "timeout_sec": 180.0,
            "retries": 30,
        }
    verifier = {"timeout_sec": 900.0, **({"env": verifier_env} if verifier_env else {})}
    return tomli_w.dumps(
        {
            "schema_version": "1.2",
            "artifacts": [],
            "task": {
                "name": f"trajectory-{family}/{task_id}",
                "description": description[:200],
                "authors": [],
                "keywords": ["trajectory_to_env", family],
            },
            "metadata": {"family": family, **metadata},
            "environment": environment,
            "verifier": verifier,
            "agent": {"timeout_sec": agent_timeout_sec},
            "solution": {"env": {}},
        }
    )


def write_files(root: Path, files: dict[str, str]) -> None:
    """Write {relative path: content}; shell scripts are made executable."""
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if path.suffix == ".sh":
            path.chmod(0o755)


def fresh_directory(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():  # left by a crashed run, with files trial containers wrote as root
        remove_as_root(path)
    path.mkdir(parents=True)
    return path
