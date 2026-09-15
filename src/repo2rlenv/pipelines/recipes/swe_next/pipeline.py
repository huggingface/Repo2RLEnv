"""Merged PRs, first-parent comparisons and original-path test extraction."""

from __future__ import annotations

from repo2rlenv.pipelines.recipes.history.pipeline import HistoryPipeline
from repo2rlenv.spec.input import PipelineName


class SWENextPipeline(HistoryPipeline):
    name = PipelineName.PR_RUNTIME
    recipe_id = "swe_next"
