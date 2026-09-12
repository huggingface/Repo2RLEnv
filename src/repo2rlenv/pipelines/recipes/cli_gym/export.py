"""Standalone environment-repair tasks, preserving the original healthy test suite."""

from __future__ import annotations

import json
import shlex
from importlib.resources import files
from pathlib import Path

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.pipelines.recipes.catalog import get_recipe


def export_task(
    *,
    base: Path,
    destination: Path,
    name: str,
    instruction: str,
    inversion,
    options,
    org: str,
    lineage: dict,
    required_tests: list[str],
    protected: dict,
    resume: bool,
) -> Path:
    assets = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(base).as_posix()
        if any(
            part in (".git", "__pycache__", ".pytest_cache")
            for part in path.relative_to(base).parts
        ):
            continue
        file = TaskFile(path.read_bytes(), executable=bool(path.stat().st_mode & 0o111))
        assets["environment/source/" + relative] = file
        if relative.startswith("tests/"):
            assets["tests/repository-tests/" + relative.removeprefix("tests/")] = file
    dockerfile = (
        f"FROM {options.base_image}\nWORKDIR /workspace\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends tmux "
        "&& rm -rf /var/lib/apt/lists/*\n"
        f"RUN python -m pip install --no-cache-dir {shlex.join(options.dependencies)}\n"
        "COPY source/ /workspace/\n"
        f"RUN {options.install_command}\n"
        "RUN rm -rf /workspace/.git /root/.cache/pip\n"
        "RUN python -m venv --copies /opt/repo2rlenv-verifier\n"
        "ENV PYTHONDONTWRITEBYTECODE=1\n"
        "RUN " + json.dumps(["/bin/bash", "-c", inversion.destruction_shell]) + "\n"
    )
    assets.update(
        {
            "environment/Dockerfile": TaskFile.text(dockerfile),
            "solution/solve.sh": TaskFile.text(inversion.recovery_shell, executable=True),
            "tests/grade.py": TaskFile(files(__package__).joinpath("grade.py").read_bytes()),
            "tests/test_results.py": TaskFile(
                files("repo2rlenv.quality").joinpath("test_results.py").read_bytes()
            ),
            "tests/test.sh": TaskFile.text(
                "#!/bin/bash\nset -eu\nexec /opt/repo2rlenv-verifier/bin/python -I /tests/grade.py\n",
                executable=True,
            ),
            "tests/contract.json": TaskFile.text(
                json.dumps(
                    {
                        "protected_source": protected,
                        "required_tests": required_tests,
                        "timeout_sec": options.test_timeout_sec,
                    }
                )
            ),
        }
    )
    return write_bundle(
        TaskBundle(
            name=name,
            org=org,
            instruction=instruction,
            files=assets,
            metadata={
                "recipe": "cli_gym",
                "recipe_version": "1",
                "pipeline": "env_repair",
                "upstream_revision": get_recipe("cli_gym").upstream["commit"],
                "quality_status": "exported",
                "reward_kinds": ["test_execution"],
                **lineage,
            },
            agent={"network_mode": "no-network"},
            verifier={"network_mode": "no-network", "user": "root"},
            verifier_timeout_sec=options.test_timeout_sec + 30,
        ),
        destination,
        resume=resume,
    )
