"""TMax's retained prompts over shared initial/final-state generation stages."""

from __future__ import annotations

from importlib.resources import files

from repo2rlenv.pipelines.recipes.terminal import templates
from repo2rlenv.pipelines.recipes.tmax.sampler import template_prompt


def design(seed, **kwargs):
    resources = files(__package__)
    return templates.design(
        seed,
        template_prompt=template_prompt(seed),
        initial_prompt=resources.joinpath("initial_prompt.md").read_text(),
        final_prompt=resources.joinpath("final_prompt.md").read_text(),
        capabilities=seed["primitive_skills"],
        **kwargs,
    )


def builder_prompt() -> str:
    return templates.builder_prompt(
        files(__package__).joinpath("environment_prompt.md").read_text()
    )
