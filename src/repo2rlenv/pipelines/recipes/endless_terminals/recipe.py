"""Retained Endless Terminals prompts and native template/test/environment order."""

from __future__ import annotations

from importlib.resources import files

from repo2rlenv.pipelines.recipes.terminal import templates


def design(seed, **kwargs):
    resources = files(__package__)
    return templates.design(
        seed,
        template_prompt=resources.joinpath("template_prompt.md").read_text(),
        initial_prompt=resources.joinpath("initial_prompt.md").read_text(),
        final_prompt=resources.joinpath("final_prompt.md").read_text(),
        capabilities=[seed["category"]],
        **kwargs,
    )


def builder_prompt() -> str:
    return templates.builder_prompt(
        files(__package__).joinpath("environment_prompt.md").read_text()
    )
