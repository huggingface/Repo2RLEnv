"""Dependency-free discovery of recipe scope; catalogued does not mean runnable."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from pydantic import BaseModel, ConfigDict


class RecipeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    title: str
    pipeline: str
    source_kinds: list[str]
    summary: str
    upstream: dict[str, Any]
    rfc: str
    target: int


# Entries are added only with an owned implementation and tests. Optional cloud
# or model libraries are imported when running, never while listing recipes.
IMPLEMENTATIONS: dict[str, str] = {
    "dataarc": "repo2rlenv.pipelines.recipes.dataarc.pipeline:DataArcPipeline",
    "cli_gym": "repo2rlenv.pipelines.recipes.cli_gym.pipeline:CLIGymPipeline",
    "terminalworld": "repo2rlenv.pipelines.recipes.terminalworld.pipeline:TerminalWorldPipeline",
    "endless_terminals": "repo2rlenv.pipelines.recipes.endless_terminals.pipeline:EndlessTerminalsPipeline",
    "tmax": "repo2rlenv.pipelines.recipes.tmax.pipeline:TMaxPipeline",
    "r2e": "repo2rlenv.pipelines.recipes.r2e.pipeline:R2EPipeline",
    "swe_flow": "repo2rlenv.pipelines.repo_reconstruct:RepoReconstructPipeline",
    "swe_gen": "repo2rlenv.pipelines.pr_to_env:PRToEnvPipeline",
    "swe_smith": "repo2rlenv.pipelines.repo_mutate:RepoMutatePipeline",
    "seta_seed2synth": "repo2rlenv.pipelines.terminal_synth:TerminalSynthesisPipeline",
    "seta_evol": "repo2rlenv.pipelines.task_evolve:TaskEvolutionPipeline",
}


@lru_cache(maxsize=1)
def recipes() -> tuple[RecipeInfo, ...]:
    data = json.loads(files(__package__).joinpath("catalog.json").read_text())
    result = tuple(RecipeInfo.model_validate(record) for record in data)
    if len({record.id for record in result}) != len(result):
        raise ValueError("Recipe catalog contains duplicate identifiers")
    return result


def get_recipe(recipe_id: str) -> RecipeInfo:
    for recipe in recipes():
        if recipe.id == recipe_id:
            return recipe
    raise ValueError(f"Unknown recipe {recipe_id!r}; use 'repo2rlenv pipelines list'")


def describe(recipe: RecipeInfo) -> dict[str, Any]:
    return {
        **recipe.model_dump(),
        "status": "experimental" if recipe.id in IMPLEMENTATIONS else "planned",
        "implemented": recipe.id in IMPLEMENTATIONS,
    }
