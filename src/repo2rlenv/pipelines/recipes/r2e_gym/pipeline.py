"""Commit mining, bug-edit filters and extracted post-change test files."""

from __future__ import annotations

from repo2rlenv.pipelines.recipes.history.pipeline import HistoryPipeline
from repo2rlenv.spec.input import PipelineName


class R2EGymPipeline(HistoryPipeline):
    name = PipelineName.COMMIT_RUNTIME
    recipe_id = "r2e_gym"
