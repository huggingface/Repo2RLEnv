"""Typed author output and standalone Harbor materialization."""

from __future__ import annotations

import ast
import json
import math
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, relative_asset_path, write_bundle
from repo2rlenv.pipelines.recipes.catalog import RecipeInfo


class EnvironmentFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str
    executable: bool


class TestWeight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    weight: float = Field(gt=0, le=1)


class TerminalDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=40, max_length=16000)
    environment_setup: str = Field(max_length=16000)
    environment_files: list[EnvironmentFile] = Field(min_length=1, max_length=100)
    tests_python: str = Field(min_length=40, max_length=50000)
    solution_shell: str = Field(min_length=20, max_length=50000)
    weights: list[TestWeight] = Field(min_length=5, max_length=10)
    self_review: str = Field(min_length=20, max_length=8000)

    @model_validator(mode="after")
    def complete_contract(self):
        paths = []
        for item in self.environment_files:
            relative_asset_path("environment/" + item.path)
            if item.path == "Dockerfile":
                raise ValueError("Environment files cannot replace the owned Dockerfile")
            paths.append(item.path)
        if len(set(paths)) != len(paths):
            raise ValueError("Environment files contain duplicate paths")
        tree = ast.parse(self.tests_python)
        tests = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        }
        weights = {item.name: item.weight for item in self.weights}
        if len(weights) != len(self.weights) or tests != weights.keys():
            raise ValueError("Weights must name each top-level pytest test exactly once")
        if not math.isclose(sum(weights.values()), 1.0, abs_tol=0.001):
            raise ValueError("Test weights must sum to one")
        if not self.solution_shell.startswith("#!/bin/bash\n"):
            raise ValueError("Reference must start with a bash shebang")
        if any(
            line.lstrip().upper().startswith(("FROM ", "USER ", "ENTRYPOINT ", "CMD "))
            for line in self.environment_setup.splitlines()
        ):
            raise ValueError("Environment setup cannot replace the base image, user or entry point")
        return self


def emit_draft(
    draft: TerminalDraft,
    destination: Path,
    *,
    name: str,
    org: str,
    recipe: RecipeInfo,
    lineage: dict,
    timeout_sec: int,
    resume: bool = False,
) -> Path:
    dockerfile = (
        "FROM python:3.12-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends bash tmux curl git jq sqlite3 "
        "&& rm -rf /var/lib/apt/lists/*\n"
        "RUN python -m pip install --no-cache-dir pytest==9.0.3 uv==0.10.9\n"
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1\n"
        "WORKDIR /workspace\nCOPY . /workspace\n"
        + draft.environment_setup.rstrip()
        + "\nWORKDIR /workspace\n"
    )
    assets = {
        "environment/" + item.path: TaskFile.text(item.content, executable=item.executable)
        for item in draft.environment_files
    }
    assets.update(
        {
            "environment/Dockerfile": TaskFile.text(dockerfile),
            "solution/solve.sh": TaskFile.text(draft.solution_shell, executable=True),
            "tests/test_outputs.py": TaskFile.text(draft.tests_python),
            "tests/weights.json": TaskFile.text(
                json.dumps({item.name: item.weight for item in draft.weights})
            ),
            "tests/grade.py": TaskFile(files(__package__).joinpath("grade.py").read_bytes()),
            "tests/test_results.py": TaskFile(
                files("repo2rlenv.quality").joinpath("test_results.py").read_bytes()
            ),
            "tests/test.sh": TaskFile.text(
                "#!/bin/bash\nset -eu\nexec /usr/local/bin/python -I /tests/grade.py\n",
                executable=True,
            ),
            "tests/contract.json": TaskFile.text(
                json.dumps(
                    {
                        "test_names": [item.name for item in draft.weights],
                        "timeout_sec": timeout_sec,
                    }
                )
            ),
        }
    )
    return write_bundle(
        TaskBundle(
            name=name,
            org=org,
            instruction=draft.instruction,
            files=assets,
            metadata={
                "recipe": recipe.id,
                "recipe_version": "1",
                "pipeline": recipe.pipeline,
                "upstream_revision": recipe.upstream["commit"],
                "reward_kinds": ["test_execution"],
                "quality_status": "exported",
                **lineage,
            },
            agent={"network_mode": "no-network"},
            verifier={"network_mode": "no-network"},
            verifier_timeout_sec=timeout_sec + 30,
        ),
        destination,
        resume=resume,
    )
