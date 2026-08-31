"""Pipeline implementations + the standardized contract."""

from repo2rlenv.pipelines.base import Pipeline, PipelineResult
from repo2rlenv.pipelines.code_instruct import CodeInstructPipeline
from repo2rlenv.pipelines.commit_runtime import CommitRuntimePipeline
from repo2rlenv.pipelines.cve_patches import CVEPatchesPipeline
from repo2rlenv.pipelines.equivalence_tests import EquivalenceTestsPipeline
from repo2rlenv.pipelines.pr_diff import PRDiffPipeline
from repo2rlenv.pipelines.pr_runtime import PRRuntimePipeline
from repo2rlenv.pipelines.pr_to_env import PrToEnvPipeline

PIPELINES: dict[str, type[Pipeline]] = {
    "pr_diff": PRDiffPipeline,
    "pr_runtime": PRRuntimePipeline,
    "pr_to_env": PrToEnvPipeline,
    "commit_runtime": CommitRuntimePipeline,
    "code_instruct": CodeInstructPipeline,
    "equivalence_tests": EquivalenceTestsPipeline,
    "cve_patches": CVEPatchesPipeline,
}

__all__ = [
    "PIPELINES",
    "CVEPatchesPipeline",
    "CodeInstructPipeline",
    "CommitRuntimePipeline",
    "EquivalenceTestsPipeline",
    "PRDiffPipeline",
    "PRRuntimePipeline",
    "Pipeline",
    "PipelineResult",
    "PrToEnvPipeline",
]
