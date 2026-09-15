"""Reconstruct terminal tasks from public recordings and observed execution effects."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.recipes.terminal.runner import run_synthesis
from repo2rlenv.pipelines.recipes.terminalworld.materialize import materialize
from repo2rlenv.pipelines.recipes.terminalworld.recipe import design
from repo2rlenv.pipelines.recipes.terminalworld.source import load_recordings
from repo2rlenv.spec.input import PipelineName, RecordingSource


class TerminalWorldPipeline:
    name = PipelineName.TERMINAL_RECONSTRUCT
    requires_bootstrap = False
    supported_languages = None
    experimental = True
    native_supported = False

    def __init__(self, input, options, bootstrap=None):
        if input.pipeline.recipe != "terminalworld" or not isinstance(
            input.source, RecordingSource
        ):
            raise ValueError("TerminalWorld requires native recording input directories")
        if input.execution is None or input.llm is None:
            raise ValueError("TerminalWorld requires a remote worker and authoring model")
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
            inputs=load_recordings(self.input.source.path),
            designer=partial(design, min_score=self.options.min_score),
            materializer=materialize,
        )
