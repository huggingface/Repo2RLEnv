"""Checkpointed synthesis with semantic and execution diversity gates."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.campaigns.events import ProgressEvent
from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.execution.artifacts import check_runtime_wheel, install_runtime, runtime_python
from repo2rlenv.execution.base import connect_worker
from repo2rlenv.execution.harbor import run_trial
from repo2rlenv.execution.lifecycle import prepare_docker, save_record
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.recipes.frontiersmith import prompts
from repo2rlenv.pipelines.recipes.frontiersmith.author import InvalidArtifact, author
from repo2rlenv.pipelines.recipes.frontiersmith.export import export_task, public_instruction
from repo2rlenv.pipelines.recipes.frontiersmith.models import (
    Design,
    Divergence,
    Infrastructure,
    Program,
    Review,
    Seed,
)
from repo2rlenv.pipelines.recipes.frontiersmith.retention import retain
from repo2rlenv.spec.input import PipelineName, SeedSource


def score_report(trial):
    paths = list(trial.result.parent.rglob("scores.json"))
    if len(paths) != 1:
        raise ValueError("Trial lacks exactly one per-case score receipt")
    report = json.loads(paths[0].read_text())
    return report


def score_vector(trial):
    return [row["reward"] for row in score_report(trial)["cases"]]


def behavioral_divergence(vectors, threshold):
    if len(vectors) < 2 or not vectors[0] or any(len(v) != len(vectors[0]) for v in vectors):
        raise ValueError("Score vectors must be nonempty and aligned")
    pairs = [
        sum(abs(a - b) for a, b in zip(left, right, strict=True)) / len(left)
        for i, left in enumerate(vectors)
        for right in vectors[i + 1 :]
    ]
    return sum(distance >= threshold for distance in pairs) / len(pairs)


class FrontierSmithPipeline:
    name = PipelineName.OPTIMIZATION_SYNTH
    requires_bootstrap = False
    native_supported = False
    experimental = True
    supported_languages = None

    def __init__(self, input, options, bootstrap=None):
        if not isinstance(input.source, SeedSource) or input.execution is None:
            raise ValueError("FrontierSmith needs seed JSON and a remote execution context")
        if input.llm is None or input.llm.provider != "openai":
            raise ValueError("FrontierSmith requires an OpenAI generation model")
        self.input, self.options = input, options
        self.on_event = lambda event: None

    def set_event_callback(self, callback):
        self.on_event = callback

    def event(self, stage, state, message):
        self.on_event(
            ProgressEvent(recipe="frontiersmith", stage=stage, state=state, message=message)
        )

    def run(self, out_dir):
        spec, options = self.input, self.options
        execution = spec.execution
        if spec.source.path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Seed shard exceeds 8 MiB")
        seeds = [Seed.model_validate(row) for row in json.loads(spec.source.path.read_text())]
        if not seeds or len({s.id for s in seeds}) != len(seeds):
            raise ValueError("Provide nonempty seeds with unique IDs")
        seeds = seeds[: options.max_candidates]
        wheel = check_runtime_wheel(execution.runtime_wheel)
        settings = spec.model_dump(mode="json")
        settings["execution"].pop("resume")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"settings": settings, "wheel": wheel, "seeds": [s.model_dump() for s in seeds]},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        run = execution.campaign_dir / "runs" / execution.run_id
        receipt = run / "run.json"
        if receipt.exists():
            record = json.loads(receipt.read_text())
            if not execution.resume or record["fingerprint"] != fingerprint:
                raise ValueError("Resume requires identical seeds, settings and runtime wheel")
            for task in record["tasks"].values():
                if inspect_bundle(Path(task["task"]))["bundle_hash"] != task["bundle_hash"]:
                    raise ValueError("Previously emitted task changed")
        else:
            record = {"fingerprint": fingerprint, "tasks": {}, "skipped": {}, "state": "running"}
            run.mkdir(parents=True, exist_ok=True)
            with receipt.open("x"):
                pass
            save_record(receipt, record)
        if record["state"] == "completed":
            return self.result(record, out_dir)
        ledger = BudgetLedger(execution.campaign_dir / "budget.sqlite3")
        worker_record = json.loads(execution.worker_receipt.read_text())
        if (
            worker_record["state"] != "running"
            or Path(worker_record["ledger"]).resolve() != ledger.path.resolve()
        ):
            raise ValueError("A running worker belonging to this campaign is required")
        worker = connect_worker(worker_record["spec"]["provider"], worker_record["worker_id"])
        deadline = min(
            datetime.now(UTC) + timedelta(seconds=execution.timeout_sec),
            datetime.fromisoformat(worker_record["started_at"])
            + timedelta(seconds=worker_record["spec"]["timeout_sec"]),
        )
        prepare_docker(worker)
        python = runtime_python(install_runtime(worker, execution.runtime_wheel, run))

        for seed in seeds:
            if len(record["tasks"]) >= options.target:
                break
            if seed.id in record["tasks"] or seed.id in record["skipped"]:
                continue
            if (deadline - datetime.now(UTC)).total_seconds() < 900:
                raise TimeoutError("Worker window too short for another candidate")
            directory = run / "candidates" / seed.id
            save_record(directory / "seed.json", seed.model_dump())

            def ask(stage, schema, prompt, payload, *, seed=seed, directory=directory):
                self.event(stage, "started", seed.title)
                format_feedback = []
                for format_attempt in range(options.max_repairs + 1):
                    key = f"{stage}-format-{format_attempt}"
                    try:
                        return author(
                            spec.llm,
                            schema,
                            prompt=prompt,
                            payload={"input": payload, "format_feedback": format_feedback},
                            path=directory / f"{key}.json",
                            ledger=ledger,
                            operation=f"frontiersmith:{execution.run_id}:{seed.id}:{key}",
                            max_tokens=options.max_tokens,
                            resume=execution.resume,
                        )
                    except InvalidArtifact as exc:
                        format_feedback.append(str(exc))
                raise InvalidArtifact(f"{stage} exhausted its structured-output repair allowance")

            design_feedback = []
            for design_attempt in range(options.max_repairs + 1):
                design = ask(
                    f"mutate-{design_attempt}",
                    Design,
                    prompts.MUTATE,
                    {"seed": seed.model_dump(), "feedback": design_feedback},
                )
                design = design.model_copy(update={"instruction": public_instruction(design)})
                review = ask(
                    f"filter-{design_attempt}", Review, prompts.FILTER, design.model_dump()
                )
                if review.approved:
                    break
                design_feedback.append(
                    {"previous_design": design.model_dump(), "review": review.model_dump()}
                )
            else:
                record["skipped"][seed.id] = "formulation_rejected"
                save_record(receipt, record)
                continue
            baseline = ask(
                "baseline",
                Program,
                prompts.SOLVE,
                {"instruction": design.instruction, "brief": design.baseline_strategy},
            )

            def sample_solution(i, *, ask=ask, design=design):
                return ask(
                    f"solution-{i}",
                    Program,
                    prompts.SOLVE,
                    {
                        "instruction": design.instruction,
                        "brief": (
                            "Develop a strong feasible strategy independently. "
                            + [
                                "Consider a constructive heuristic.",
                                "Consider bounded local improvement.",
                                "Consider a different global or multistart approach.",
                            ][i % 3]
                        ),
                    },
                )

            with ThreadPoolExecutor(
                max_workers=max(1, min(options.solutions, spec.llm.max_concurrent))
            ) as pool:
                solutions = list(pool.map(sample_solution, range(options.solutions)))
            divergence = ask(
                "divergence",
                Divergence,
                prompts.DIVERGENCE,
                {
                    "instruction": design.instruction,
                    "solutions": [s.model_dump() for s in solutions],
                },
            )
            expected = len(solutions) * (len(solutions) - 1) // 2
            if len(divergence.distinct_pairs) != expected:
                raise ValueError("Divergence review returned the wrong number of pairs")
            if sum(divergence.distinct_pairs) / expected < options.min_divergence:
                record["skipped"][seed.id] = "low_semantic_divergence"
                save_record(receipt, record)
                continue

            feedback = []
            success = False
            for attempt in range(options.max_repairs + 1):
                generator = ask(
                    f"generator-{attempt}",
                    Program,
                    prompts.GENERATOR,
                    {
                        "design": design.model_dump(),
                        "solutions": [s.model_dump() for s in solutions],
                        "feedback": feedback,
                        "attempts_remaining": options.max_repairs - attempt,
                    },
                )
                scorer = ask(
                    f"scorer-{attempt}",
                    Program,
                    prompts.SCORER,
                    {
                        "design": design.model_dump(),
                        "generator": generator.code,
                        "feedback": feedback,
                        "attempts_remaining": options.max_repairs - attempt,
                    },
                )
                feasibility = ask(
                    f"feasibility-{attempt}",
                    Program,
                    prompts.FEASIBILITY,
                    {"instruction": design.instruction, "feedback": feedback},
                )
                infrastructure = Infrastructure(
                    generator=generator.code, scorer=scorer.code, feasibility=feasibility.code
                )
                checked = ask(
                    f"review-{attempt}",
                    Review,
                    prompts.REVIEW,
                    {
                        "design": design.model_dump(),
                        "infrastructure": infrastructure.model_dump(),
                        "baseline": baseline.model_dump(),
                        "solutions": [s.model_dump() for s in solutions],
                    },
                )
                if not checked.approved:
                    feedback.append(
                        {"review": checked.model_dump(), "previous": infrastructure.model_dump()}
                    )
                    continue
                name = "frontiersmith-" + seed.id
                lineage = {
                    "seed_id": seed.id,
                    "seed_source": seed.source,
                    "seed_license": seed.license,
                    "author_model": spec.llm.qualified_name,
                }

                def emit(
                    solution,
                    destination,
                    *,
                    design=design,
                    infrastructure=infrastructure,
                    name=name,
                    lineage=lineage,
                ):
                    return export_task(
                        design,
                        infrastructure,
                        solution,
                        destination,
                        name=name,
                        org=spec.output.org,
                        seed=options.seed,
                        lineage=lineage,
                        resume=execution.resume,
                    )

                trial_prefix = (
                    "fs-"
                    + hashlib.sha256(
                        f"{execution.run_id}:{seed.id}:{attempt}".encode()
                    ).hexdigest()[:16]
                )

                def trial(
                    task,
                    label,
                    agent="oracle",
                    *,
                    seed=seed,
                    directory=directory,
                    attempt=attempt,
                    trial_prefix=trial_prefix,
                ):
                    self.event("execute", "started", f"{seed.title}: {label}")
                    return run_trial(
                        worker,
                        task,
                        directory / f"execution-{attempt}" / label,
                        trial_id=f"{trial_prefix}-{label}",
                        agent=agent,
                        python=python,
                        resume=execution.resume,
                        timeout_sec=600,
                    )

                base_task = emit(baseline, directory / f"baseline-{attempt}")
                nop = trial(base_task, "nop", "nop")
                base = trial(base_task, "baseline")
                samples = [
                    trial(emit(s, directory / f"sample-{attempt}-{i}"), f"sample-{i}")
                    for i, s in enumerate(solutions)
                ]
                trials = [nop, base, *samples]
                if not all(t.completed for t in trials):
                    # Retain concrete verifier stderr for bounded infrastructure repair.
                    errors = []
                    for t in trials:
                        errors.append(
                            {
                                "exception": t.exception_type,
                                "logs": {
                                    p.name: p.read_text()[-8000:]
                                    for p in t.result.parent.rglob("*.txt")
                                    if p.name
                                    in {
                                        "test-stdout.txt",
                                        "test-stderr.txt",
                                        "stderr.txt",
                                        "stdout.txt",
                                    }
                                },
                            }
                        )
                    feedback.append(
                        {"execution_errors": errors, "previous": infrastructure.model_dump()}
                    )
                    continue
                if nop.reward != 0:
                    raise ValueError("Missing submission received nonzero reward")
                vectors = [score_vector(t) for t in samples]
                eligible = [
                    i
                    for i, sample in enumerate(samples)
                    if all(
                        row["status"] == "completed" and row["feasible"] is True
                        for row in score_report(sample)["cases"]
                    )
                ]
                baseline_completed = all(
                    row["status"] == "completed" and row["feasible"] is True
                    for row in score_report(base)["cases"]
                )
                if not baseline_completed or len(eligible) < 2:
                    record["skipped"][seed.id] = "insufficient_successful_programs"
                    break
                spread = behavioral_divergence(
                    [vectors[i] for i in eligible], options.min_score_spread
                )
                best = max(eligible, key=lambda i: samples[i].reward)
                if (
                    spread < options.min_divergence
                    or samples[best].reward <= base.reward
                    or samples[best].reward <= 0
                ):
                    feedback.append(
                        {
                            "execution_diversity": {
                                "baseline": base.reward,
                                "sample_rewards": [t.reward for t in samples],
                                "vectors": vectors,
                                "distinct_pair_fraction": spread,
                            },
                            "required_action": "Find legal input cases that distinguish competing algorithms. Preserve the public objective and scorer formula. If none exist within the public constraints, report the limitation; do not manipulate rewards.",
                            "previous": infrastructure.model_dump(),
                        }
                    )
                    if attempt == options.max_repairs:
                        record["skipped"][seed.id] = "low_execution_diversity_or_improvement"
                    continue
                reference = emit(solutions[best], directory / f"reference-{attempt}")
                repeat = trial(reference, "repeat")
                if not repeat.completed or score_vector(repeat) != vectors[best]:
                    record["skipped"][seed.id] = "reference_not_repeatable"
                    break
                # Export the same tested bundle; no post-validation rewriting of prompts/tests.
                task = reference
                evidence = {
                    "task": str((out_dir / name).resolve()),
                    "bundle_hash": inspect_bundle(task)["bundle_hash"],
                    "baseline_reward": base.reward,
                    "reference_reward": samples[best].reward,
                    "sample_rewards": [t.reward for t in samples],
                    "score_vectors": vectors,
                    "semantic_divergence": sum(divergence.distinct_pairs) / expected,
                    "behavioral_divergence": spread,
                    "construction_verified": True,
                    "explicit_feasibility": True,
                    "rollout_status": "not_run",
                    "attempts": attempt + 1,
                    "reference_index": best,
                    "reference_repeat_result": str(repeat.result.resolve()),
                }
                if len(record["tasks"]) < options.rollout_tasks:
                    self.event("rollout", "started", seed.title)
                    rollout = run_trial(
                        worker,
                        task,
                        directory / "rollout",
                        trial_id=trial_prefix + "-rollout",
                        agent="responses",
                        model=spec.llm,
                        ledger=ledger,
                        reservation_usd="1.50",
                        max_turns=8,
                        max_tokens=4096,
                        python=python,
                        resume=execution.resume,
                        timeout_sec=600,
                    )
                    evidence.update(
                        rollout_status="completed" if rollout.completed else "failed",
                        rollout_reward=rollout.reward,
                        rollout_result=str(rollout.result.resolve()),
                    )
                save_record(directory / "quality.json", evidence)
                retain(task, out_dir / name, directory / "quality.json")
                record["tasks"][seed.id] = evidence
                save_record(receipt, record)
                success = True
                self.event(
                    "export", "completed", f"{seed.title}: reference {samples[best].reward:.3f}"
                )
                break
            if not success and seed.id not in record["skipped"]:
                record["skipped"][seed.id] = "infrastructure_repair_exhausted"
            save_record(receipt, record)
        record["state"] = "completed"
        save_record(receipt, record)
        return self.result(record, out_dir)

    @staticmethod
    def result(record, out_dir):
        skipped = Counter(record["skipped"].values())
        return PipelineResult(
            len(record["tasks"]) + sum(skipped.values()),
            len(record["tasks"]),
            sum(skipped.values()),
            out_dir,
            dict(skipped),
        )
