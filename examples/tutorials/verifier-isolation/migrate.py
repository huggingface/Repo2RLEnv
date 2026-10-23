"""Create a new local revision of the sampled HF ML concatenation task.

This only copies and edits files. Build and execute the output on Modal or Daytona.
It is a worked migration for one task, not a translator for arbitrary pytest suites.
"""

from __future__ import annotations

import argparse
import shutil
import tomllib
from pathlib import Path

import repo2rlenv
from repo2rlenv.quality.loop.artifacts import refresh_identity, task_identity


def migrate(source: Path, destination: Path) -> None:
    source, destination = source.resolve(), destination.resolve()
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError("Use a new output directory outside the input task")
    task_identity(source)  # Reject a modified bundle that still claims an old identity.
    config = tomllib.loads((source / "task.toml").read_text())
    if config.get("task", {}).get("name") != "repo2rlenv/tasksmith-3d762a2acfee":
        raise ValueError("This example only migrates the sampled Accelerate concatenation task")
    old_copy = "COPY grade.py test_driver.py test_results.py contract.json test.sh /tests/"
    dockerfile = (source / "tests/Dockerfile").read_text()
    if old_copy not in dockerfile or "RUN chmod 755 /tests && chmod 644 /tests/*" not in dockerfile:
        raise ValueError("Verifier layout changed; inspect it before adapting this migration")
    shutil.copytree(source, destination)
    tests = destination / "tests"
    template = Path(__file__).parent
    package = Path(repo2rlenv.__file__).parent
    # Historical exports can inline this validation rather than expose a function.
    # Use the maintained standalone implementation, not an assumed old API.
    shutil.copyfile(package / "pipelines/recipes/swe_smith/grade.py", tests / "submission_guard.py")
    for filename in ("grade.py", "value_worker.py"):
        shutil.copyfile(template / filename, tests / filename)
    shutil.copyfile(
        package / "pipelines/recipes/repository/isolated_call.py", tests / "isolated_call.py"
    )
    dockerfile = dockerfile.replace(
        old_copy,
        "COPY grade.py submission_guard.py isolated_call.py test.sh /tests/\n"
        "COPY value_worker.py /opt/value_worker.py",
    ).replace(
        "RUN chmod 755 /tests && chmod 644 /tests/*",
        "RUN chmod 700 /tests && chmod 644 /opt/value_worker.py && rm -rf /workspace/tests",
    )
    (tests / "Dockerfile").write_text(dockerfile)
    for filename in ("test_driver.py", "test_results.py", "contract.json"):
        (tests / filename).unlink()
    instruction = destination / "instruction.md"
    instruction.write_text(
        instruction.read_text().replace(
            "Grading runs the relevant repository tests in a fresh environment, using your submitted source files.",
            "Grading checks concatenation and data-loader outputs in a fresh environment, "
            "using your submitted source files.",
        )
    )
    refresh_identity(destination)  # Preserve the old revision, invalidate old acceptance evidence.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    migrate(arguments.source, arguments.destination)
