"""TMax legacy task generation through its retained sampler and distinct stages."""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.recipes.terminal.runner import run_synthesis
from repo2rlenv.pipelines.recipes.tmax import recipe
from repo2rlenv.pipelines.recipes.tmax.preflight import initial_state
from repo2rlenv.pipelines.recipes.tmax.sampler import sample_inputs
from repo2rlenv.spec.input import PipelineName, SeedSource


class TMaxPipeline:
    name = PipelineName.TERMINAL_SYNTH
    requires_bootstrap = False
    supported_languages = None
    experimental = True
    native_supported = False

    def __init__(self, input, options, bootstrap=None):
        if input.pipeline.recipe != "tmax" or not isinstance(input.source, SeedSource):
            raise ValueError("TMax requires its sampler JSON as a seeds input")
        if input.execution is None or input.llm is None:
            raise ValueError("TMax requires remote execution and an authoring model")
        self.input, self.options = input, options
        self.on_event = lambda event: None

    def set_event_callback(self, callback) -> None:
        self.on_event = callback

    def run(self, out_dir: Path) -> PipelineResult:
        return run_synthesis(
            self.input,
            self.options,
            out_dir,
            self.on_event,
            inputs=sample_inputs(self.input.source.path),
            designer=recipe.design,
            builder_prompt=recipe.builder_prompt(),
            preflight=initial_state,
            agent_user="user",
        )
