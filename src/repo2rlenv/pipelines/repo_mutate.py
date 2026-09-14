"""Repository mutation family: owned SWE-smith procedural generation."""

from __future__ import annotations

import hashlib
import json

from repo2rlenv.execution.artifacts import runtime_python
from repo2rlenv.execution.base import connect_worker
from repo2rlenv.execution.harbor import run_trial
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
        directory = run / "tasks" / candidate["id"]
        task = export_candidate(
            generation,
            candidate,
            self.options,
            issue.issue,
            directory / "draft",
            org=self.input.output.org,
            resume=execution.resume,
        )
        receipt = json.loads(execution.worker_receipt.read_text())
        worker = connect_worker(receipt["spec"]["provider"], receipt["worker_id"])
        python = runtime_python(hashlib.sha256(execution.runtime_wheel.read_bytes()).hexdigest())
        prefix = hashlib.sha256(f"{execution.run_id}:{candidate['id']}".encode()).hexdigest()[:20]
        self.event("harbor", "started", "Fresh standalone baseline and reference execution")
        trials = [
            run_trial(
                worker,
                task,
                directory / agent,
                trial_id=f"smith-{prefix}-{agent}",
                agent=agent,
                python=python,
                resume=execution.resume,
            )
            for agent in ("nop", "oracle")
        ]
        if not all(trial.completed for trial in trials) or [trial.reward for trial in trials] != [
            0,
            1,
        ]:
            raise ValueError("SWE-smith task did not pass Harbor baseline/reference checks")
        return export_candidate(
            generation,
            candidate,
            self.options,
            issue.issue,
            out_dir,
            org=self.input.output.org,
            resume=execution.resume,
        )
