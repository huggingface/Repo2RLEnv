"""Pipeline implementations + the standardized contract."""

from repo2rlenv.pipelines.base import Pipeline, PipelineResult
from repo2rlenv.pipelines.pr_diff import PRDiffPipeline
from repo2rlenv.pipelines.pr_runtime import PRRuntimePipeline

PIPELINES: dict[str, type[Pipeline]] = {
    "pr_diff": PRDiffPipeline,
    "pr_runtime": PRRuntimePipeline,
}

__all__ = ["PIPELINES", "Pipeline", "PipelineResult", "PRDiffPipeline", "PRRuntimePipeline"]
