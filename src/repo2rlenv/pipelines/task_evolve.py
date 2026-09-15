"""Evolve supplied Harbor tasks through an explicit curriculum strategy."""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.spec.input import GenerationInput, PipelineName, TaskSource
from repo2rlenv.spec.recipe_options import TaskEvolutionOptions


class TaskEvolutionPipeline:
    name = PipelineName.TASK_EVOLVE
    requires_bootstrap = False
    supported_languages = None
    experimental = True
    native_supported = False

    def __init__(self, input: GenerationInput, options: TaskEvolutionOptions, bootstrap=None):
        if input.pipeline.recipe != "seta_evol" or not isinstance(input.source, TaskSource):
            raise ValueError("task_evolve requires the SETA evolution recipe and task input")
        if input.execution is None or input.llm is None:
            raise ValueError("Evolution requires a remote worker and authoring model")
        self.input, self.options = input, options
        self.on_event = lambda event: None

    def set_event_callback(self, callback) -> None:
        self.on_event = callback

    def run(self, out_dir: Path) -> PipelineResult:
        from repo2rlenv.pipelines.recipes.seta_evol import recipe
        from repo2rlenv.pipelines.recipes.terminal.runner import run_synthesis

        parents = recipe.load_parents(self.input.source.path, self.options)
        return run_synthesis(
            self.input,
            self.options,
            out_dir,
            self.on_event,
            inputs=parents,
            designer=recipe.design,
            builder_prompt=recipe.builder_prompt(),
        )
