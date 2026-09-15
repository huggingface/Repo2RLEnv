"""Seeded sampling from TMax's retained legacy taxonomy and complexity axes."""

from __future__ import annotations

import json
import random
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SamplerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_kind: Literal["legacy"] = "legacy"
    count: int = Field(default=40, ge=1, le=1000)
    seed: int = 24
    domains: list[str] | None = None
    languages: list[str] | None = None


def sample_inputs(path: Path) -> list[dict]:
    settings = SamplerInput.model_validate_json(path.read_text())
    data = json.loads(files(__package__).joinpath("taxonomy.json").read_text())
    taxonomy = data["SKILL_TAXONOMY"]
    languages, weights = zip(*data["TASK_LANGUAGES"], strict=True)
    if settings.domains is not None and (
        not settings.domains or not set(settings.domains) <= taxonomy.keys()
    ):
        raise ValueError("Unknown or empty TMax domain selection")
    if settings.languages is not None and (
        not settings.languages or not set(settings.languages) <= set(languages)
    ):
        raise ValueError("Unknown or empty TMax language selection")
    rng = random.Random(settings.seed)
    records = []
    for draw in range(settings.count * 100):
        domain = rng.choice(list(taxonomy))
        types = taxonomy[domain]
        skill_type = rng.choice(list(types))
        all_skills = [skill for group in types.values() for skill in group]
        skills = rng.sample(all_skills, min(rng.randint(3, 5), len(all_skills)))
        complexity = rng.choice(data["TASK_COMPLEXITY"][:3])
        commands = rng.choice(data["COMMAND_COMPLEXITY"])
        scenario = rng.choice(data["DOMAIN_SCENARIOS"][domain])
        language = rng.choices(languages, weights=weights, k=1)[0]
        anchors = data["REAL_SOFTWARE_ANCHORS"].get(domain)
        anchor = rng.choice(anchors) if anchors and rng.random() < 0.35 else None
        if settings.domains is not None and domain not in settings.domains:
            continue
        if settings.languages is not None and language not in settings.languages:
            continue
        records.append(
            {
                "source": "tmax_legacy_taxonomy",
                "title": f"{domain}: {skill_type} ({draw})",
                "domain": domain,
                "skill_type": skill_type,
                "primitive_skills": skills,
                "task_complexity": complexity,
                "command_complexity": commands,
                "scenario": scenario,
                "language": language,
                "anchor": anchor,
                "corpus_kind": "legacy",
                "fixture_kind": "text_only",
                "verifier_kind": "exact_text",
                "sampler_seed": settings.seed,
                "draw": draw,
            }
        )
        if len(records) == settings.count:
            return records
    raise ValueError("Sampler selection is too restrictive for its bounded draw budget")


def template_prompt(seed: dict) -> str:
    data = json.loads(files(__package__).joinpath("taxonomy.json").read_text())
    return (
        files(__package__)
        .joinpath("template_prompt.md")
        .read_text()
        .replace("{{domain_label}}", seed["domain"].replace("_", " ").title())
        .replace("{{module}}", data["DOMAIN_MODULES"][seed["domain"]])
        .replace("{{v2_block}}", "")
    )
