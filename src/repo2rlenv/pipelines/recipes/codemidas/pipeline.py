"""Agentic source discovery, observed tests and independent review to Harbor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from importlib.resources import files

from repo2rlenv.execution.artifacts import runtime_python
from repo2rlenv.execution.base import connect_worker
from repo2rlenv.execution.generation import run_generator
from repo2rlenv.execution.harbor import run_trial
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.pipelines.recipes.codemidas.models import Feature, Review, Verifier
from repo2rlenv.pipelines.recipes.codemidas.source import (
    existing_reconstructions,
    implementation_pair,
    reconstruction_identity,
    validate_assertions,
)
from repo2rlenv.pipelines.recipes.repository.export import export_repository_task
from repo2rlenv.pipelines.recipes.repository.runner import RepositoryGenerationPipeline
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.spec.input import PipelineName
from repo2rlenv.tasksmith.author.artifact import artifact_stage
from repo2rlenv.tasksmith.author.bridge import ProviderOutputError
from repo2rlenv.tasksmith.author.budget import AuthorBudget


class ConstructionExhausted(RuntimeError):
    """Stop this candidate without treating a known rejection as a provider fault."""


class CodeMidasPipeline(RepositoryGenerationPipeline):
    name = PipelineName.REPO_RECONSTRUCT
    recipe_id = "codemidas"
    worker_module = "repo2rlenv.pipelines.recipes.codemidas.worker"

    def __init__(self, input, options, bootstrap=None):
        super().__init__(input, options, bootstrap)
        if (
            input.llm.qualified_name != options.author_model
            or input.llm.endpoint
            or input.llm.fallback
        ):
            raise ValueError(
                "CodeMidas requires its explicit direct OpenAI author model, without fallback"
            )
        if options.stack_manifest:
            from repo2rlenv.pipelines.recipes.codemidas.stack import read_manifest

            row = read_manifest(options.stack_manifest)["row"]
            if (
                input.repo.url != "https://github.com/" + row["repo_path"]
                or input.repo.ref != row["commit_id"]
            ):
                raise ValueError(
                    "Repository identity and revision must match the Stack row exactly"
                )

    def source_identity(self):
        if self.options.stack_manifest:
            return {
                "stack_manifest_sha256": hashlib.sha256(
                    self.options.stack_manifest.read_bytes()
                ).hexdigest()
            }
        return {}

    def worker_configuration(self, run):
        config = super().worker_configuration(run)
        if self.options.stack_manifest:
            from repo2rlenv.pipelines.recipes.codemidas.stack import read_manifest

            config["stack_source"] = read_manifest(self.options.stack_manifest)
        return config

    def author_export(self, generation, candidate, ledger, run, out_dir):
        try:
            return asyncio.run(self._author_export(generation, candidate, ledger, run, out_dir))
        except (ConstructionExhausted, ProviderOutputError) as exc:
            raise ValueError(str(exc)) from exc

    async def _author_export(self, generation, candidate, ledger, run, out_dir):
        execution = self.input.execution
        key = candidate["id"]
        directory = run / "tasks" / key
        receipt = json.loads(execution.worker_receipt.read_text())
        worker = connect_worker(receipt["spec"]["provider"], receipt["worker_id"])
        python = runtime_python(hashlib.sha256(execution.runtime_wheel.read_bytes()).hexdigest())
        prefix = hashlib.sha256(f"{execution.run_id}:{key}".encode()).hexdigest()[:20]
        budget = RunBudget(ledger, f"codemidas:{prefix}", str(self.options.candidate_budget_usd))
        deadline = time.time() + min(2400, execution.timeout_sec)
        observations_path = directory / "observations.json"
        shell_calls = (
            json.loads(observations_path.read_text())["observations"]
            if observations_path.exists()
            else []
        )

        async def shell(command, timeout_sec=120):
            if not isinstance(command, str) or len(command) > 20000:
                raise ValueError("Use a bounded shell command")
            timeout_sec = max(1, min(int(timeout_sec), 120))
            name = "codemidas-observe-" + uuid.uuid4().hex
            argv = [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,size=128m",
                "--cpus",
                "1",
                "--memory",
                f"{self.options.test_memory_mb}m",
                "--pids-limit",
                "128",
                "-w",
                "/workspace",
                candidate["image_digest"],
                "timeout",
                str(timeout_sec),
                "sh",
                "-c",
                command,
            ]
            try:
                result = await asyncio.to_thread(worker.exec, argv, timeout=timeout_sec + 20)
                observation = {
                    "id": len(shell_calls) + 1,
                    "command": command,
                    "returncode": result.returncode,
                    "output": result.stdout[-20000:],
                }
                shell_calls.append(observation)
                save_record(directory / "observations.json", {"observations": shell_calls})
                return json.dumps(observation)
            finally:
                await asyncio.to_thread(worker.exec, ["docker", "rm", "-f", name], timeout=30)

        async def stage(
            schema,
            label,
            inputs,
            prompt_name,
            model,
            validate=None,
            use_shell=False,
            initial_draft_key=None,
        ):
            self.event(label, "started", candidate["anchor"])
            root = directory / label
            context_path = root / "context.json"
            if context_path.exists():
                previous = json.loads(context_path.read_text())

                def immutable(value):
                    return {
                        k: v for k, v in value.items() if k not in {"observations", "execution"}
                    }

                if immutable(previous) != immutable(inputs):
                    raise ValueError("Stage context changed; use a new run identity")
                inputs = previous
            else:
                save_record(context_path, inputs)
            return await artifact_stage(
                schema=schema,
                stage=label,
                inputs=inputs,
                system=files(__package__).joinpath(prompt_name + ".md").read_text(),
                prompt=json.dumps(inputs),
                root=root,
                budget=AuthorBudget(budget, root / "costs", label),
                model=model,
                runtime="openai",
                max_cost=self.options.candidate_budget_usd,
                max_turns=self.options.max_turns,
                deadline=deadline,
                shell=shell if use_shell else None,
                validate=validate,
                initial_draft_key=initial_draft_key,
            )

        async def check_feature(value):
            implementation_pair(generation / "base", value, self.options.source_paths)
            if not any(
                symbol.path == candidate["path"] and symbol.qualified_name == candidate["anchor"]
                for symbol in value.symbols
            ):
                raise ValueError(
                    "Include the anchor implementation in the selected coherent feature"
                )

        feature = await stage(
            Feature,
            "design",
            {"candidate": candidate, "source_paths": self.options.source_paths},
            "design",
            self.options.author_model,
            check_feature,
            True,
        )
        defective, _ = implementation_pair(generation / "base", feature, self.options.source_paths)
        feature_hash = reconstruction_identity(candidate["repo"], candidate["ref"], defective)
        if feature_hash in existing_reconstructions(out_dir):
            raise ValueError("This missing implementation already has a task in the collection")
        evaluations = [
            json.loads(path.read_text())
            for path in sorted(directory.glob("execution-*/*/evaluation.json"))
        ]

        async def check_verifier(value):
            validate_assertions(feature, value)
            if len(evaluations) >= self.options.max_rounds:
                raise ConstructionExhausted("CodeMidas construction repair allowance exhausted")
            attempt = len(evaluations)
            output = await asyncio.to_thread(
                run_generator,
                worker,
                python=python,
                module=self.worker_module,
                config={
                    "mode": "evaluate",
                    "options": self.options.model_dump(mode="json"),
                    "feature": feature.model_dump(),
                    "verifier": value.model_dump(),
                    "generation": "/evidence/generation/" + execution.run_id,
                    "image_digest": candidate["image_digest"],
                },
                directory=directory / f"execution-{attempt}",
                job_id=f"cm-{prefix}-{attempt}",
                timeout_sec=self.options.test_timeout_sec * 2 + 90,
                resume=execution.resume,
            )
            if output is None:
                raise RuntimeError(
                    "CodeMidas remote test execution failed; inspect retained evidence"
                )
            evaluation = json.loads((output / "evaluation.json").read_text())
            evaluations.append(evaluation)
            if not evaluation["contrast"]:
                raise ValueError(json.dumps(evaluation))

        test_inputs = {
            "feature": feature.model_dump(),
            "candidate": candidate,
            "learner_instruction": feature.task_instruction(),
        }
        for revision in range(self.options.max_rounds):
            suffix = "" if not revision else f"-revision-{revision}"
            verifier = await stage(
                Verifier,
                "tests" + suffix,
                test_inputs,
                "tests",
                self.options.author_model,
                check_verifier,
                True,
                "previous_verifier" if revision else None,
            )
            review = await stage(
                Review,
                "review" + suffix,
                {
                    "feature": feature.model_dump(),
                    "learner_instruction": feature.task_instruction(),
                    "verifier": verifier.model_dump(),
                    "observations": shell_calls,
                    "execution": evaluations[-1],
                    "source": {
                        symbol.path: (generation / "base" / symbol.path).read_text()
                        for symbol in feature.symbols
                    },
                },
                "review",
                self.options.reviewer_model,
            )
            if review.approved:
                break
            if review.repair_target == "contract" and revision + 1 < self.options.max_rounds:
                selected = feature.symbols

                async def check_revision(value, selected=selected):
                    await check_feature(value)
                    if value.symbols != selected:
                        raise ValueError("A contract repair must preserve the selected feature")

                feature = await stage(
                    Feature,
                    f"design-revision-{revision + 1}",
                    {
                        "candidate": candidate,
                        "source_paths": self.options.source_paths,
                        "previous_feature": feature.model_dump(),
                        "review_feedback": review.issues,
                        "repair_policy": (
                            "Correct only the identified contract discrepancy using observed "
                            "public API behavior. Preserve selected symbols and feature scope. "
                            "No solver has run; do not tailor requirements to model success."
                        ),
                    },
                    "design",
                    self.options.author_model,
                    check_revision,
                    True,
                    "previous_feature",
                )
            test_inputs = {
                "feature": feature.model_dump(),
                "learner_instruction": feature.task_instruction(),
                "candidate": candidate,
                "previous_verifier": verifier.model_dump(),
                "review_feedback": review.issues,
            }
        else:
            raise ConstructionExhausted(
                "Independent assertion review rejected: " + "; ".join(review.issues)
            )
        if not evaluations:  # Artifact resume still requires its bound execution receipt.
            outputs = sorted(directory.glob("execution-*/*/evaluation.json"))
            if not outputs:
                raise ValueError("Committed tests lack execution evidence")
            evaluations = [json.loads(path.read_text()) for path in outputs]
        defective, reference = implementation_pair(
            generation / "base", feature, self.options.source_paths
        )
        from repo2rlenv.pipelines.recipes.codemidas.sanitize import public_profile

        profile = public_profile(generation / "base", self.options, candidate["private_paths"])
        kwargs = dict(
            base=generation / "base",
            defective=defective,
            reference=reference,
            options=profile,
            instruction=feature.task_instruction(),
            name="codemidas-" + key,
            org=self.input.output.org,
            contrast=evaluations[-1]["contrast"],
            verifier_source={"test_codemidas_generated.py": verifier.test_code.encode()},
            resume=execution.resume,
            metadata={
                "recipe": "codemidas",
                "recipe_version": "1",
                "pipeline": "repo_reconstruct",
                "paper": "https://arxiv.org/abs/2609.22068",
                "repository": candidate["repo"],
                "source_revision": candidate["ref"],
                "source_kind": candidate.get("source_kind", "github"),
                "source_provenance": candidate.get("source_provenance", {}),
                "author_model": self.options.author_model,
                "reviewer_model": self.options.reviewer_model,
                "assertion_review": "passed",
                "instruction_contract_version": "1",
                "reconstruction_identity": feature_hash,
            },
        )
        task = export_repository_task(destination=directory / "draft", **kwargs)
        for agent, count, reward in (("nop", 2, 0), ("oracle", 4, 1)):
            for index in range(count):
                self.event("harbor", "started", f"{agent} {index + 1}/{count}")
                evidence = await asyncio.to_thread(
                    run_trial,
                    worker,
                    task,
                    directory / f"{agent}-{index}",
                    trial_id=f"cm-{prefix}-{agent}-{index}",
                    agent=agent,
                    python=python,
                    resume=execution.resume,
                    timeout_sec=900,
                )
                if not evidence.completed or evidence.reward != reward:
                    raise ValueError(f"Harbor {agent} trial {index} failed its consistency gate")
        # Preserve the exact verified bytes; stage evidence is stored next to the task.
        from repo2rlenv.pipelines.recipes.codemidas.retention import retain

        output = retain(task, out_dir / task.name, directory)
        save_record(
            directory / "quality.json",
            {
                "execution_verified": True,
                "baseline_runs": 2,
                "reference_runs": 4,
                "assertion_review": review.model_dump(),
                "rollout_review": "not_run",
                "curriculum_selection": "not_run",
                "task": str(output.resolve()),
                "cost": budget.totals(),
            },
        )
        return output
