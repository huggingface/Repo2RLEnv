"""Released SCALER family expansion, without model calls or training dependencies."""

from __future__ import annotations

import hashlib
import json

from repo2rlenv.execution.artifacts import runtime_python
from repo2rlenv.execution.base import connect_worker
from repo2rlenv.execution.harbor import run_trial
from repo2rlenv.pipelines.recipes.repository.runner import RemoteGenerationPipeline
from repo2rlenv.pipelines.recipes.scaler.export import export_task
from repo2rlenv.pipelines.recipes.scaler.families import load_families
from repo2rlenv.spec.input import FamilySource, PipelineName


class ScalerPipeline(RemoteGenerationPipeline):
    name = PipelineName.REASONING_SYNTH
    recipe_id = "scaler"
    worker_module = "repo2rlenv.pipelines.recipes.scaler.worker"
    supported_languages = None

    def __init__(self, input, options, bootstrap=None):
        super().__init__(input, options, bootstrap)
        if not isinstance(input.source, FamilySource):
            raise ValueError("SCALER requires a native family JSON mapping")
        self.families, self.source_sha256 = load_families(input.source.path)

    def source_identity(self):
        return {"families_sha256": self.source_sha256}

    def worker_configuration(self, run):
        return {
            "families": self.families,
            "source_sha256": self.source_sha256,
            "options": self.options.model_dump(mode="json"),
        }

    def author_export(self, generation, candidate, ledger, run, out_dir):
        execution = self.input.execution
        directory = run / "tasks" / candidate["id"]
        task = export_task(
            candidate, directory / "draft", self.input.output.org, resume=execution.resume
        )
        receipt = json.loads(execution.worker_receipt.read_text())
        worker = connect_worker(receipt["spec"]["provider"], receipt["worker_id"])
        python = runtime_python(hashlib.sha256(execution.runtime_wheel.read_bytes()).hexdigest())
        prefix = hashlib.sha256(f"{execution.run_id}:{candidate['id']}".encode()).hexdigest()[:20]
        self.event(
            "harbor", "started", f"{candidate['family']} · difficulty {candidate['difficulty']}"
        )
        trials = [
            run_trial(
                worker,
                task,
                directory / agent,
                trial_id=f"scaler-{prefix}-{agent}",
                agent=agent,
                python=python,
                resume=execution.resume,
            )
            for agent in ("nop", "oracle")
        ]
        if not all(trial.completed for trial in trials) or [trial.reward for trial in trials] != [
            -1,
            1,
        ]:
            raise ValueError(
                "SCALER export did not retain its native -1/+1 baseline/reference reward"
            )
        return export_task(candidate, out_dir, self.input.output.org, resume=execution.resume)
