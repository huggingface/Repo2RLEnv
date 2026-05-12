"""Pipeline implementations + the standardized contract."""

from repo2rlenv.pipelines.base import Pipeline, PipelineResult
from repo2rlenv.pipelines.code_instruct import CodeInstructPipeline
from repo2rlenv.pipelines.commit_runtime import CommitRuntimePipeline
from repo2rlenv.pipelines.cve_patches import CVEPatchesPipeline
from repo2rlenv.pipelines.equivalence_tests import EquivalenceTestsPipeline
from repo2rlenv.pipelines.mutation_bugs import MutationBugsPipeline
from repo2rlenv.pipelines.pr_diff import PRDiffPipeline
from repo2rlenv.pipelines.pr_runtime import PRRuntimePipeline
from repo2rlenv.pipelines.pr_stream import PRStreamPipeline
from repo2rlenv.pipelines.refactor_synthesis import RefactorSynthesisPipeline

PIPELINES: dict[str, type[Pipeline]] = {
    "pr_diff": PRDiffPipeline,
    "pr_runtime": PRRuntimePipeline,
    "pr_stream": PRStreamPipeline,
    "commit_runtime": CommitRuntimePipeline,
    "mutation_bugs": MutationBugsPipeline,
    "code_instruct": CodeInstructPipeline,
    "equivalence_tests": EquivalenceTestsPipeline,
    "cve_patches": CVEPatchesPipeline,
    "refactor_synthesis": RefactorSynthesisPipeline,
}

__all__ = [
    "PIPELINES",
    "CVEPatchesPipeline",
    "CodeInstructPipeline",
    "CommitRuntimePipeline",
    "EquivalenceTestsPipeline",
    "MutationBugsPipeline",
    "PRDiffPipeline",
    "PRRuntimePipeline",
    "PRStreamPipeline",
    "Pipeline",
    "PipelineResult",
    "RefactorSynthesisPipeline",
]
