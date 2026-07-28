"""Read an emitted Repo2RLEnv dataset back off disk.

`repo2rlenv generate` writes Harbor task directories; this module turns one of
those directories back into a value object the OpenEnv runtime can serve, and
discovers many of them under a dataset root.

Nothing here executes anything — see `sandbox.py` for that.

A Repo2RLEnv task is a Harbor task plus the `[metadata.repo2env]` table, which
is what lets us do better than a generic Harbor runtime: when a dataset has been
pushed with `repo2rlenv push`, `[metadata.repo2env.reproducibility]` names a
pullable image and we skip the build entirely.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TASK_CONFIG_FILE = "task.toml"
INSTRUCTION_FILE = "instruction.md"

DEFAULT_AGENT_TIMEOUT_S = 1800.0
DEFAULT_VERIFIER_TIMEOUT_S = 300.0

#: How deep `TaskSet` descends looking for task directories. Enough for a flat
#: `generate --out` directory, the `tasks/<id>/` layout `push` stages to the
#: Hub, and one level of grouping above either.
_MAX_DISCOVERY_DEPTH = 4

#: `[metadata.repo2env.reproducibility].mode` values written by `push`.
#: Only `registry` guarantees `image_ref` is pullable.
REPRO_MODE_REGISTRY = "registry"


class TaskFormatError(ValueError):
    """Raised when a directory is not a well-formed Repo2RLEnv task."""


@dataclass(frozen=True)
class Repo2RLEnvTask:
    """One emitted task directory, parsed.

    Attributes:
        task_id: Directory path relative to the dataset root; what `reset()` selects on.
        path: Absolute path to the task directory.
        name: `[task].name`, the qualified `<org>/<slug>` form.
        description: `[task].description`.
        instruction: `instruction.md` — the prompt handed to the agent.
        schema_version: Top-level `version` (Harbor spells this `schema_version`).
        pipeline: Which synthesis pipeline produced the task.
        reward_kinds: Reward kinds the pipeline declared it emits.
        content_hash: Content address of the task, for provenance.
        repro_mode: `reproducibility.mode` — registry / inline_dockerfile / local_only.
        image_ref: Image named by `reproducibility.image_ref`, if any.
        metadata: The full `[metadata.repo2env]` table, verbatim.
        agent_timeout_s: `[agent].timeout_sec`.
        verifier_timeout_s: `[verifier].timeout_sec`.
    """

    task_id: str
    path: Path
    name: str
    description: str
    instruction: str
    schema_version: str
    pipeline: str = ""
    reward_kinds: tuple[str, ...] = ()
    content_hash: str = ""
    repro_mode: str = ""
    image_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S
    verifier_timeout_s: float = DEFAULT_VERIFIER_TIMEOUT_S

    # --- layout -------------------------------------------------------------

    @property
    def environment_dir(self) -> Path:
        return self.path / "environment"

    @property
    def tests_dir(self) -> Path:
        return self.path / "tests"

    @property
    def solution_dir(self) -> Path:
        return self.path / "solution"

    @property
    def dockerfile(self) -> Path | None:
        """`environment/Dockerfile`, or None for a text-only (lite) task."""
        candidate = self.environment_dir / "Dockerfile"
        return candidate if candidate.is_file() else None

    @property
    def test_script(self) -> Path | None:
        """`tests/test.sh` — the verifier, absent on lite tasks."""
        candidate = self.tests_dir / "test.sh"
        return candidate if candidate.is_file() else None

    @property
    def solve_script(self) -> Path | None:
        """`solution/solve.sh` — the oracle shim that applies patch.diff."""
        candidate = self.solution_dir / "solve.sh"
        return candidate if candidate.is_file() else None

    @property
    def oracle_diff(self) -> Path | None:
        """`solution/patch.diff` — the canonical oracle artifact."""
        candidate = self.solution_dir / "patch.diff"
        return candidate if candidate.is_file() else None

    @property
    def pullable_image(self) -> str | None:
        """The image to pull instead of building, when one is guaranteed to exist.

        Only `mode = "registry"` means `repo2rlenv push` actually pushed the
        image somewhere pullable. `local_only` and `inline_dockerfile` both
        carry an `image_ref` that may exist on no machine but the one that
        generated the task, so those build from `environment/Dockerfile`.
        """
        if self.repro_mode == REPRO_MODE_REGISTRY and self.image_ref:
            return self.image_ref
        return None

    @property
    def runnable(self) -> bool:
        """Whether the task can be executed and graded.

        `pr_diff` with `emit_harbor_env=False` emits neither a Dockerfile nor a
        verifier: it is a stored-diff task, scored client-side against
        `solution/patch.diff` rather than by running anything.
        """
        return self.dockerfile is not None or self.pullable_image is not None

    def summary(self) -> dict[str, Any]:
        """A JSON-serializable digest, surfaced in observations and state."""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "description": self.description,
            "pipeline": self.pipeline,
            "reward_kinds": list(self.reward_kinds),
            "content_hash": self.content_hash,
            "repro_mode": self.repro_mode,
            "image_ref": self.image_ref,
            "runnable": self.runnable,
            "has_verifier": self.test_script is not None,
            "has_oracle": self.solve_script is not None,
        }

    # --- loading ------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path, task_id: str | None = None) -> Repo2RLEnvTask:
        """Parse the task directory at `path`.

        Raises:
            TaskFormatError: if `task.toml` is missing or unparsable.
        """
        task_dir = Path(path).expanduser().resolve()
        config_path = task_dir / TASK_CONFIG_FILE
        if not config_path.is_file():
            raise TaskFormatError(f"{task_dir} is not a task directory: no {TASK_CONFIG_FILE}")

        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TaskFormatError(f"could not parse {config_path}: {exc}") from exc

        task_table = _table(config, "task")
        metadata = _table(config, "metadata")
        repo2env = _table(metadata, "repo2env")
        repro = _table(repo2env, "reproducibility")

        instruction_path = task_dir / INSTRUCTION_FILE
        instruction = (
            instruction_path.read_text(encoding="utf-8")
            if instruction_path.is_file()
            else str(task_table.get("description", ""))
        )

        reward_kinds = repo2env.get("reward_kinds")
        return cls(
            task_id=task_id or task_dir.name,
            path=task_dir,
            name=str(task_table.get("name", task_dir.name)),
            description=str(task_table.get("description", "")),
            instruction=instruction,
            # We write `version`; Harbor's own tasks spell it `schema_version`.
            schema_version=str(config.get("version") or config.get("schema_version") or "unknown"),
            pipeline=str(repo2env.get("pipeline", "")),
            reward_kinds=tuple(str(k) for k in reward_kinds)
            if isinstance(reward_kinds, list)
            else (),
            content_hash=str(repo2env.get("content_hash", "")),
            repro_mode=str(repro.get("mode", "")),
            image_ref=(str(repro["image_ref"]) if repro.get("image_ref") else None),
            metadata=repo2env,
            agent_timeout_s=_timeout(config, "agent", DEFAULT_AGENT_TIMEOUT_S),
            verifier_timeout_s=_timeout(config, "verifier", DEFAULT_VERIFIER_TIMEOUT_S),
        )


class TaskSet:
    """Discovers Repo2RLEnv tasks under a dataset root.

    Any directory holding a `task.toml` is a task, and discovery does not
    descend into one once found. That covers the flat layout `generate --out`
    writes, the `tasks/<id>/` layout `push` stages, and a single task directory
    passed directly.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._discovered: dict[str, Path] | None = None

    def task_ids(self) -> list[str]:
        """Sorted identifiers of every task under the root."""
        return sorted(self._task_dirs())

    def refresh(self) -> None:
        """Forget the cached scan so the next lookup re-reads the filesystem."""
        self._discovered = None

    def get(self, task_id: str) -> Repo2RLEnvTask:
        """Load one task by identifier, or by bare directory name when unique.

        Raises:
            KeyError: if nothing matches, or a bare name is ambiguous.
        """
        available = self._task_dirs()
        if task_id in available:
            return Repo2RLEnvTask.load(available[task_id], task_id=task_id)

        matches = [tid for tid in available if Path(tid).name == task_id]
        if len(matches) == 1:
            return Repo2RLEnvTask.load(available[matches[0]], task_id=matches[0])
        if len(matches) > 1:
            raise KeyError(
                f"task id {task_id!r} is ambiguous under {self.root}; use one of {sorted(matches)}"
            )
        raise KeyError(
            f"unknown task {task_id!r} under {self.root}; available: {sorted(available) or '<none>'}"
        )

    def _task_dirs(self) -> dict[str, Path]:
        if self._discovered is None:
            self._discovered = self._scan()
        return self._discovered

    def _scan(self) -> dict[str, Path]:
        if not self.root.is_dir():
            return {}
        if (self.root / TASK_CONFIG_FILE).is_file():
            return {self.root.name: self.root}

        found: dict[str, Path] = {}
        stack = [(self.root, 0)]
        while stack:
            directory, depth = stack.pop()
            if depth > _MAX_DISCOVERY_DEPTH:
                continue
            for child in sorted(directory.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if (child / TASK_CONFIG_FILE).is_file():
                    found[child.relative_to(self.root).as_posix()] = child
                else:
                    stack.append((child, depth + 1))
        return found


def _table(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _timeout(config: dict[str, Any], section: str, fallback: float) -> float:
    raw = _table(config, section).get("timeout_sec")
    try:
        parsed = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
