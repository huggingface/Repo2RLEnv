"""Export a mutation as a standalone task with a separate Harbor verifier."""

from __future__ import annotations

import json
import shlex
from importlib.resources import files
from pathlib import Path, PurePosixPath

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.pipelines.recipes.swe_smith.options import SWESmithOptions

RECIPE_VERSION = "1"
UPSTREAM_REVISION = "9b74ac08118a85c39c356802f7961893af73e07f"


def export_candidate(
    generation: Path,
    candidate: dict,
    options: SWESmithOptions,
    instruction: str,
    destination: Path,
    *,
    org: str,
    resume: bool = False,
) -> Path:
    """Both build contexts begin with defective source, including private tests.

    Only source files are collected into a fresh grading environment. Interpreter,
    tests, configuration and reward writer are never collected from the learner.
    """
    base = generation / "base"
    evidence = generation / "candidates" / candidate["id"]
    source_file = candidate["source_file"]
    assets: dict[str, TaskFile] = {}
    collected = []
    source_roots = [PurePosixPath(path) for path in options.source_paths]
    hidden_roots = [PurePosixPath(path) for path in options.test_paths]
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ValueError("Task snapshots cannot contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Task snapshots require regular files")
        relative = PurePosixPath(path.relative_to(base).as_posix())
        if (
            any(part in {".git", "__pycache__", ".pytest_cache"} for part in relative.parts)
            or path.suffix == ".pyc"
        ):
            raise ValueError(f"Snapshot contains a forbidden cache/history asset: {relative}")
        content = (
            (evidence / "mutated.py").read_bytes()
            if str(relative) == source_file
            else path.read_bytes()
        )
        asset = TaskFile(content, bool(path.stat().st_mode & 0o111))
        assets[f"tests/source/{relative}"] = asset
        private_test = any(relative == root or root in relative.parents for root in hidden_roots)
        if not private_test:
            assets[f"environment/source/{relative}"] = asset
        if (
            not private_test
            and path.suffix == ".py"
            and any(relative == root or root in relative.parents for root in source_roots)
        ):
            collected.append(str(relative))
    if source_file not in collected:
        raise ValueError("Mutated source must be within the submitted Python source paths")
    if set(collected) & {str(root) for root in hidden_roots}:
        raise ValueError("Source and private test paths overlap")

    build = (
        f"FROM {options.base_image}\nWORKDIR /workspace\n"
        + (
            f"RUN python -m pip install --no-cache-dir {shlex.join(options.dependencies)}\n"
            if options.dependencies
            else ""
        )
        + "COPY source /workspace\n"
        f"RUN {options.install_command}\n"
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1\n"
    )
    assets["environment/Dockerfile"] = TaskFile.text(
        build
        + "RUN apt-get update && apt-get install -y --no-install-recommends tmux && rm -rf /var/lib/apt/lists/*\n"
        "RUN useradd -m -u 1000 learner && chown -R learner:learner /workspace\n"
    )
    assets["tests/Dockerfile"] = TaskFile.text(
        build + "RUN useradd -m -u 1001 grader\n"
        "COPY grade.py test_driver.py test_results.py contract.json test.sh /tests/\n"
        "RUN chmod 755 /tests && chmod 644 /tests/*\n"
    )
    assets["tests/test.sh"] = TaskFile.text(
        "#!/bin/sh\nset -eu\nexec /usr/local/bin/python -I /tests/grade.py\n", executable=True
    )
    assets["tests/grade.py"] = TaskFile(files(__package__).joinpath("grade.py").read_bytes())
    assets["tests/test_driver.py"] = TaskFile(
        files(__package__).joinpath("test_driver.py").read_bytes()
    )
    assets["tests/test_results.py"] = TaskFile(
        files("repo2rlenv.quality").joinpath("test_results.py").read_bytes()
    )
    assets["tests/contract.json"] = TaskFile.text(
        json.dumps(
            {
                "submitted_files": collected,
                "test_paths": options.test_paths,
                "expected_passes": candidate["contrast"]["FAIL_TO_PASS"]
                + candidate["contrast"]["PASS_TO_PASS"],
                "timeout_sec": options.test_timeout_sec,
            },
            sort_keys=True,
        )
    )
    assets["solution/reference.py"] = TaskFile((evidence / "original.py").read_bytes())
    assets["solution/solve.sh"] = TaskFile.text(
        "#!/bin/sh\nset -eu\ncp /solution/reference.py "
        + shlex.quote("/workspace/" + source_file)
        + "\n",
        executable=True,
    )
    instruction = instruction.rstrip() + (
        "\n\nWork in `/workspace`. Submit your fix in the existing Python source files under "
        + ", ".join(f"`{root}`" for root in options.source_paths)
        + ". Preserve the other public behavior. The environment is offline; dependencies are preinstalled. "
        "Grading runs the repository's test suite in a fresh environment, using your submitted source files.\n"
    )
    bundle = TaskBundle(
        name="swe-smith-" + candidate["id"],
        org=org,
        instruction=instruction,
        files=assets,
        metadata={
            "recipe": "swe_smith",
            "recipe_version": RECIPE_VERSION,
            "pipeline": "repo_mutate",
            "upstream_revision": UPSTREAM_REVISION,
            "repository": candidate["repo"],
            "source_revision": candidate["ref"],
            "mutation_id": candidate["id"],
            "reward_kinds": ["test_execution"],
            "quality_status": "exported",
            "fail_to_pass_count": len(candidate["contrast"]["FAIL_TO_PASS"]),
            "pass_to_pass_count": len(candidate["contrast"]["PASS_TO_PASS"]),
        },
        agent={"user": "learner", "network_mode": "no-network"},
        verifier={
            "user": "root",
            "network_mode": "no-network",
            "environment_mode": "separate",
            "environment": {"network_mode": "no-network", "cpus": 1, "memory_mb": 2048},
        },
        artifacts=[{"source": "/workspace/" + path} for path in collected],
        verifier_timeout_sec=options.test_timeout_sec + 30,
    )
    return write_bundle(bundle, destination, resume=resume)
