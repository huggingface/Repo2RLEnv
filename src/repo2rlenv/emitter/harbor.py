"""Write Harbor-compliant task directories.

The lite path emits a minimal Harbor task:
  task.toml + instruction.md + solution/patch.diff
No environment/, no tests/. Reward kind = "diff_similarity" only.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The output FORMAT (task.toml schema, directory layout, /logs/verifier/reward.txt
contract, [metadata] tables) is defined by:

  Harbor Framework (Laude Institute / Terminal-Bench creators)
  https://github.com/harbor-framework/harbor    (Apache-2.0)
  https://www.harborframework.com/docs/tasks

We emit Harbor's format directly so any Harbor-compatible runtime, agent
harness, or downstream framework (OpenReward, SkyRL via Harbor, etc.) can
consume our datasets unchanged. We do NOT depend on the `harbor` Python
package — we generate the file format from scratch. The format itself is a
spec (data layout); using it does not require a license grant. Repo2RLEnv-
specific provenance lives inside Harbor's free-form `[metadata]` table under
the namespaced subtable `[metadata.repo2env]`.

Released under Apache-2.0.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w


@dataclass(slots=True)
class HarborTask:
    name: str
    org: str
    description: str
    instruction: str
    oracle_diff: str
    repo2env: dict[str, Any]
    difficulty: str = "medium"
    category: str = "bugfix"
    keywords: list[str] = field(default_factory=list)
    # Optional — only set for sandbox-required pipelines (e.g. pr_runtime).
    # Lite tasks (pr_diff) leave these as None; Harbor falls back to its own
    # default env / test runner for those.
    environment_dockerfile: str | None = None
    test_script: str | None = None


def _content_hash(task: HarborTask) -> str:
    h = hashlib.sha256()
    h.update(task.instruction.encode("utf-8"))
    h.update(b"\0")
    h.update(task.oracle_diff.encode("utf-8"))
    return f"sha256:{h.hexdigest()}"


def write_harbor_task(task: HarborTask, dest_dir: Path) -> Path:
    """Materialize the task directory at dest_dir/<task.name>. Returns the path."""
    task_path = dest_dir / task.name
    task_path.mkdir(parents=True, exist_ok=True)

    # task.toml
    repo2env = dict(task.repo2env)
    repo2env.setdefault("spec_version", "0.1.0")
    repo2env.setdefault("content_hash", _content_hash(task))
    # Default reward kinds — sandbox-required tasks override with
    # test_execution as the primary signal
    if task.test_script is not None:
        repo2env.setdefault("reward_kinds", ["test_execution", "diff_similarity"])
    else:
        repo2env.setdefault("reward_kinds", ["diff_similarity"])

    # Harbor's task.toml requires `task.name` in `<org>/<name>` format —
    # validated at load-time by harbor.models.task.config.PackageInfo. We
    # keep the filesystem-safe slug (with `__` for path safety) as the
    # directory name, but emit the schema-required `org/slug` form in
    # task.toml so harbor accepts the task.
    qualified_name = f"{task.org}/{task.name}"
    payload: dict[str, Any] = {
        "version": "1.0",
        "task": {
            "name": qualified_name,
            "description": task.description,
        },
        "metadata": {
            "difficulty": task.difficulty,
            "category": task.category,
            "keywords": task.keywords,
            "repo2env": repo2env,
        },
        "agent": {"timeout_sec": 1800.0},
        "verifier": {"timeout_sec": 300.0},
    }
    (task_path / "task.toml").write_bytes(tomli_w.dumps(payload).encode("utf-8"))

    # instruction.md
    (task_path / "instruction.md").write_text(task.instruction, encoding="utf-8")

    # solution/patch.diff — canonical SWE-bench-style oracle (what trainers consume)
    sol_dir = task_path / "solution"
    sol_dir.mkdir(exist_ok=True)
    (sol_dir / "patch.diff").write_text(task.oracle_diff, encoding="utf-8")

    # solution/solve.sh — Harbor's oracle agent runs this script inside the
    # container; it should leave the working tree in the "fixed" state. We
    # `git apply` the canonical patch.diff so we keep one oracle artifact
    # (patch.diff) and just provide the execution shim Harbor needs.
    (sol_dir / "solve.sh").write_text(
        "#!/bin/bash\n"
        "set -euxo pipefail\n"
        "cd /workspace\n"
        "git config --global --add safe.directory /workspace\n"
        # Harbor uploads the whole solution/ dir into the container under
        # /solution; the patch.diff sits next to this script.
        'PATCH="$(dirname "$0")/patch.diff"\n'
        'git apply --verbose --reject "$PATCH"\n',
        encoding="utf-8",
    )
    (sol_dir / "solve.sh").chmod(0o755)

    # Optional environment/Dockerfile + tests/test.sh — written only for
    # sandbox-required tasks (pr_runtime, future commit_runtime, etc.).
    if task.environment_dockerfile is not None:
        env_dir = task_path / "environment"
        env_dir.mkdir(exist_ok=True)
        (env_dir / "Dockerfile").write_text(task.environment_dockerfile, encoding="utf-8")
    if task.test_script is not None:
        tests_dir = task_path / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test.sh").write_text(task.test_script, encoding="utf-8")
        # mark executable; harbor expects test.sh to be runnable
        (tests_dir / "test.sh").chmod(0o755)

    return task_path
