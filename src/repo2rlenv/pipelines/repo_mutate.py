"""Repository mutation family: owned SWE-smith procedural generation."""

from __future__ import annotations

from repo2rlenv.pipelines.recipes.repository.runner import RepositoryGenerationPipeline
from repo2rlenv.pipelines.recipes.swe_smith.export import export_candidate
from repo2rlenv.pipelines.recipes.swe_smith.issue import write_issue
from repo2rlenv.spec.input import PipelineName


class RepoMutatePipeline(RepositoryGenerationPipeline):
    name = PipelineName.REPO_MUTATE
    recipe_id = "swe_smith"
    worker_module = "repo2rlenv.pipelines.recipes.swe_smith.worker"

    def author_export(self, generation, candidate, ledger, run, out_dir):
        execution = self.input.execution
        issue = write_issue(
            generation,
            candidate,
            self.input.llm,
            ledger,
            run / "models" / f"{candidate['id']}.json",
            operation_id=f"issue:{execution.run_id}:{candidate['id']}",
            resume=execution.resume,
        )
        return export_candidate(
            generation,
            candidate,
            self.options,
            issue.issue,
            out_dir,
            org=self.input.output.org,
            resume=execution.resume,
        )
