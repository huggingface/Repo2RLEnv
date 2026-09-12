"""Concrete reasoning instances with a private reference answer and native reward scale."""

from __future__ import annotations

import json
from importlib.resources import files

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.pipelines.recipes.catalog import get_recipe


def export_task(candidate, destination, org, *, resume=False):
    recipe = get_recipe("scaler")
    bundle = TaskBundle(
        name="scaler-" + candidate["id"],
        org=org,
        instruction=candidate["instruction"],
        files={
            "environment/Dockerfile": TaskFile.text(
                "FROM python:3.12-slim\nRUN apt-get update && apt-get install -y --no-install-recommends tmux && rm -rf /var/lib/apt/lists/*\nWORKDIR /workspace\nRUN useradd -m -u 1000 learner && touch answer.txt && chown -R learner:learner /workspace\n"
            ),
            "tests/Dockerfile": TaskFile.text(
                "FROM python:3.12-slim\nRUN python -m pip install --no-cache-dir math-verify==0.8.0\nWORKDIR /workspace\nRUN touch answer.txt\nCOPY grade.py reference.json test.sh /tests/\nRUN chmod 755 /tests/test.sh\n"
            ),
            "tests/test.sh": TaskFile.text(
                "#!/bin/sh\nset -eu\nexec /usr/local/bin/python -I /tests/grade.py\n",
                executable=True,
            ),
            "tests/grade.py": TaskFile(files(__package__).joinpath("grade.py").read_bytes()),
            "tests/reference.json": TaskFile.text(json.dumps(candidate["reference_answer"])),
            "tests/UPSTREAM_LICENSE": TaskFile(
                files(__package__).joinpath("UPSTREAM_LICENSE").read_bytes()
            ),
            "solution/solve.sh": TaskFile.text(
                "#!/bin/sh\nset -eu\ncp /solution/answer.txt /workspace/answer.txt\n",
                executable=True,
            ),
            "solution/answer.txt": TaskFile.text(candidate["reference_answer"] + "\n"),
        },
        metadata={
            "recipe": "scaler",
            "recipe_version": "1",
            "pipeline": recipe.pipeline,
            "upstream_revision": recipe.upstream["commit"],
            "reward_kinds": ["answer_equivalence"],
            "reward_min": -1,
            "reward_max": 1,
            "quality_status": "exported",
            "domain": "algorithmic_reasoning",
            "generation_scope": "released_family_expansion",
            **{
                key: candidate[key]
                for key in (
                    "family",
                    "difficulty",
                    "actual_seed",
                    "source_sha256",
                    "instance_sha256",
                    "reference_code_sha256",
                )
            },
        },
        agent={"user": "learner", "network_mode": "no-network"},
        verifier={
            "user": "root",
            "network_mode": "no-network",
            "environment_mode": "separate",
            "environment": {"network_mode": "no-network", "cpus": 1, "memory_mb": 2048},
        },
        artifacts=[{"source": "/workspace/answer.txt"}],
        verifier_timeout_sec=60,
    )
    return write_bundle(bundle, destination, resume=resume)
