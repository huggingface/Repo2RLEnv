"""Endless Terminals' native independent category, complexity and context draws."""

from __future__ import annotations

import json
import random
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SamplerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(default=40, ge=1, le=1000)
    seed: int = 24
    categories: list[str] | None = None


def sample_inputs(path: Path) -> list[dict]:
    settings = SamplerInput.model_validate_json(path.read_text())
    taxonomy = json.loads(files(__package__).joinpath("taxonomy.json").read_text())
    categories = taxonomy["TASK_CATEGORIES"]
    if settings.categories is not None:
        if not settings.categories or not set(settings.categories) <= set(categories):
            raise ValueError("Unknown or empty Endless Terminals category selection")
        categories = [item for item in categories if item in settings.categories]
    rng = random.Random(settings.seed)
    records = []
    for index in range(settings.count):
        category = rng.choice(categories)
        complexity = rng.choice(taxonomy["COMPLEXITY_LEVELS"])
        scenario = rng.choice(taxonomy["SCENARIO_CONTEXTS"])
        records.append(
            {
                "source": "endless_terminals_taxonomy",
                "title": f"{category} ({index})",
                "category": category,
                "task_complexity": complexity,
                "scenario": scenario,
                "sampler_seed": settings.seed,
                "draw": index,
                "request": f"Write a new task focusing on {category}. Complexity: {complexity}. "
                f"Scenario: {scenario}. Be very specific about the output format in the task "
                "description that the automated test will check. Write the task description "
                "in a way that a user might ask an AI assistant. The task should be a realistic "
                "end-to-end scenario that an AI agent could perform in a Linux terminal.",
            }
        )
    return records
