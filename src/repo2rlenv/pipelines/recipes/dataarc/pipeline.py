"""DataArc's released terminal synthesis branch under the shared Harbor boundary."""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.recipes.dataarc import recipe
from repo2rlenv.pipelines.recipes.terminal.runner import run_synthesis
from repo2rlenv.spec.input import PipelineName, SeedSource


class DataArcPipeline:
    name = PipelineName.TERMINAL_SYNTH
    requires_bootstrap = False
    supported_languages = None
    experimental = True
    native_supported = False

    def __init__(self, input, options, bootstrap=None):
        if input.pipeline.recipe != "dataarc" or not isinstance(input.source, SeedSource):
            raise ValueError("DataArc requires a directory of Harbor seed tasks")
        if input.execution is None or input.llm is None:
            raise ValueError("DataArc requires remote execution and an authoring model")
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
            inputs=recipe.load_seeds(self.input.source.path, self.options),
            designer=recipe.design,
            materializer=recipe.materialize,
        )
