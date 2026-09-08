"""Independent task review, protected controls, rollouts and evidence-based admission."""

from __future__ import annotations

import difflib
import json
from pathlib import Path

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.evaluate import TAMPER
from repo2rlenv.curation.review_evidence import project_trace
from repo2rlenv.tasksmith.authoring import Construction
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.contracts import bind_contracts
from repo2rlenv.tasksmith.models import (
    AdmissionContext,
    EvidenceRef,
    PRIdentity,
    TrialRecord,
    TrialRequirement,
    admission_reasons,
)
from repo2rlenv.tasksmith.review import comprehension_review, final_review, verifier_critic
from repo2rlenv.tasksmith.trials import TrialOutcome, run_trial
from repo2rlenv.tasksmith.worker import save_json
from repo2rlenv.ui import console


def paired_witness(baseline: TrialOutcome, oracle: TrialOutcome) -> dict:
    """The same grader must observe changed behavior from exact transferred source."""
    identities = ("task_digest", "provider", "profile_digest", "materialization_digest")
    reasons = []
    if any(getattr(baseline, key) != getattr(oracle, key) for key in identities):
        reasons.append("Baseline and oracle use different task/profile/materialization identities")
    if baseline.reward != 0 or oracle.reward != 1:
        reasons.append("A healthy base failure and reference pass are required")
    for label, trial in (("baseline", baseline), ("oracle", oracle)):
        if trial.error or not trial.collection_verified or not trial.cleanup_confirmed:
            reasons.append(label + " execution/transfer/cleanup is incomplete")
        value = trial.source_observation
        if not value or not {"value", "package_origin", "origin_sha256"} <= value.keys():
            reasons.append(label + " source observation is missing")
        else:
            inventory = json.loads((Path(trial.path) / "verifier/collection.json").read_text())
            relative = value["package_origin"].removeprefix("/workspace/")
            if relative not in inventory or inventory[relative]["sha256"] != value["origin_sha256"]:
                reasons.append(label + " observed import does not match transferred bytes")
    if (
        baseline.source_observation
        and oracle.source_observation
        and baseline.source_observation.get("value") == oracle.source_observation.get("value")
    ):
        reasons.append("Probe did not witness a change in the PR's behavior")
    changed = []
    if baseline.collection_verified and oracle.collection_verified:
        before = json.loads((Path(baseline.path) / "verifier/collection.json").read_text())
        after = json.loads((Path(oracle.path) / "verifier/collection.json").read_text())
        changed = [p for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)]
        if not changed:
            reasons.append("Reference did not change collected source")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "changed_source": changed,
        **{k: getattr(baseline, k) for k in identities},
        "baseline": baseline.source_observation,
        "oracle": oracle.source_observation,
    }


def _failure_route(row: TrialOutcome) -> dict:
    """Construction diagnostics are repairable, but never become behavioral rewards."""
    construction = row.construction_error is not None
    incomplete = not construction and (row.error is not None or row.reward is None)
    return {
        "status": "incomplete" if incomplete else "needs_repair",
        "repairable": not incomplete,
        "stage": "construction"
        if construction
        else ("infrastructure" if incomplete else "execution"),
    }


def _trial_text(outcome: TrialOutcome, *, trace=False) -> str:
    folder = Path(outcome.path)
    chunks = [json.dumps(outcome.model_dump(), indent=2)]
    for file in (
        "verifier/details.json",
        "verifier/pytest-output.txt",
        "verifier/source-observation-error.json",
        "agent/oracle.txt",
        "agent/oracle-setup.json",
        "agent/control.json",
        "agent/exit-code.txt",
    ):
        path = folder / file
        if path.is_file():
            chunks.append(file + "\n" + path.read_text())
    for path in sorted((folder.parent / "resources").glob("*.json")):
        chunks.append("provider lifecycle receipt: " + path.name + "\n" + path.read_text())
    collection = folder.parent / "collection-receipt.json"
    if collection.is_file():
        chunks.append("controller collection receipt\n" + collection.read_text())
    if trace:
        raw = (folder / "agent/trace.jsonl").read_bytes()
        projection, receipt = project_trace(
            raw, outcome.kind + (":" + outcome.model if outcome.model else "")
        )
        chunks.extend([projection, json.dumps(receipt)])
    return "\n\n".join(chunks)


def submission_changes(baseline: TrialOutcome, outcomes: dict[str, TrialOutcome]) -> str:
    before_root = Path(baseline.path) / "artifacts/workspace"
    before = json.loads((Path(baseline.path) / "verifier/collection.json").read_text())
    records = []
    for name, trial in outcomes.items():
        after_root = Path(trial.path) / "artifacts/workspace"
        after = json.loads((Path(trial.path) / "verifier/collection.json").read_text())
        changes = []
        for path in sorted(set(before) | set(after)):
            if before.get(path) == after.get(path):
                continue
            left = (before_root / path).read_text() if path in before else ""
            right = (after_root / path).read_text() if path in after else ""
            changes.append(
                {
                    "path": path,
                    "before": before.get(path),
                    "after": after.get(path),
                    "diff": "".join(
                        difflib.unified_diff(
                            left.splitlines(True),
                            right.splitlines(True),
                            fromfile="base/" + path,
                            tofile=name + "/" + path,
                        )
                    ),
                }
            )
        records.append({"trial": name, "changes": changes})
    text = json.dumps(records, indent=2)
    if len(text) > 160000:
        raise ValueError(
            "Complete submission evidence exceeds review profile; explicit profile revision required"
        )
    return text


async def validate_candidate(
    config: TasksmithConfig,
    source: dict,
    construction: dict,
    root: Path,
    deadline: float,
    journal,
    lease,
    campaign_root: Path,
) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    task = Path(construction["task_path"])
    revision_digest = construction["emitter"]["task_digest"]
    if digest_task(task) != revision_digest:
        raise ValueError("Frozen task bytes changed before validation")
    value = Construction.model_validate(construction["artifact"])
    budget = config.budget(source["id"])
    operations = [
        o
        for o in journal.operations(lease.pr_id)
        if o.key.stage == "validation"
        and o.key.revision_digest == revision_digest
        and o.status == "claimed"
    ]
    if len(operations) != 1:
        raise ValueError("Validation must own one durable journal effect")
    operation_id = operations[0].key.operation_id
    evidence_refs = {}

    def evidence(name: str, text: str):
        ref = journal.commit_artifact(
            lease, operation_id, name=name, data=text.encode(), media_type="text/plain"
        )
        evidence_refs[name] = EvidenceRef(revision_digest=revision_digest, artifact=ref)
        return ref

    patch_path = root.parent / "gold.patch"
    patch = evidence("source_patch", patch_path.read_text())
    bundle = bind_contracts(config, source, construction, patch)
    save_json(root / "contracts.json", bundle.model_dump(mode="json"))
    instruction = (task / "instruction.md").read_text()
    if instruction != bundle.task.useful_outcome:
        raise ValueError("Public contract differs from the emitted task instruction")
    tests = (task / "tests/test_contract.py").read_text()
    # A path-only inventory is intentionally sufficient for the public-only pass.
    # Public content review may be added without exposing private gold/tests.
    visible_files = value.contract.source_paths
    comprehension = await comprehension_review(
        config=config,
        budget=budget,
        root=root / "comprehension",
        deadline=deadline,
        instruction=instruction,
        visible_files=visible_files,
        reference_hashes=(source["head_sha"],),
    )
    if comprehension.status != "pass":
        return {
            "status": "needs_repair",
            "repairable": True,
            "stage": "comprehension",
            "review": comprehension.model_dump(mode="json"),
        }
    critic = await verifier_critic(
        config=config,
        budget=budget,
        root=root / "critic",
        deadline=deadline,
        contract=value.contract,
        artifacts={
            "instruction": instruction,
            "protected_tests": tests,
            "oracle_script": (task / "solution/solve.sh").read_text(),
            "source_excerpts": patch_path.read_text(),
            "provenance": value.model_dump_json(indent=2),
        },
    )
    if critic.status != "pass":
        return {
            "status": "needs_repair",
            "repairable": True,
            "stage": "verifier_critic",
            "review": critic.model_dump(mode="json"),
        }

    outcomes: dict[str, TrialOutcome] = {}
    required: list[TrialRequirement] = []

    async def trial(name, kind, expected_reward, *, script=None, oracle=False, model=None):
        required.append(
            TrialRequirement(id=name, kind=kind, expected_reward=expected_reward, model=model)
        )
        console.info(f"Tasksmith {source['id']}: {name}")
        target = root / "trials" / name
        outcome = await run_trial(
            task,
            config,
            budget,
            target,
            kind=kind,
            model=model,
            script=script,
            oracle=oracle,
            deadline=deadline,
        )
        outcomes[name] = outcome
        save_json(
            root / "trial-inventory.json",
            {
                "required": [r.model_dump(mode="json") for r in required],
                "outcomes": {k: v.model_dump() for k, v in outcomes.items()},
            },
        )
        return outcome

    def bad(name, expected):
        row = outcomes[name]
        return row.error or row.reward is None or (expected is not None and row.reward != expected)

    def repair(name):
        return {
            **_failure_route(outcomes[name]),
            "failed_trial": name,
            "outcome": outcomes[name].model_dump(),
            "evidence": _trial_text(outcomes[name]),
            "all_trials": {k: v.model_dump() for k, v in outcomes.items()},
        }

    await trial("baseline", "baseline", 0, script="true")
    if bad("baseline", 0):
        return repair("baseline")
    await trial("oracle-0", "oracle", 1, oracle=True)
    if bad("oracle-0", 1):
        return repair("oracle-0")
    witness = paired_witness(outcomes["baseline"], outcomes["oracle-0"])
    save_json(root / "source-witness.json", witness)
    if not witness["passed"]:
        return {
            "status": "needs_repair",
            "repairable": True,
            "stage": "source_witness",
            "evidence": witness,
        }
    await trial("oracle-1", "oracle", 1, oracle=True)
    if bad("oracle-1", 1):
        return repair("oracle-1")
    for kind, prefix, controls, expected in (
        ("negative_control", "negative", value.contract.mutations, 0),
        ("positive_control", "positive", value.contract.equivalents, 1),
    ):
        for i, control in enumerate(controls):
            name = f"{prefix}-{i}"
            await trial(name, kind, expected, script=control.script, oracle=True)
            if bad(name, expected):
                return repair(name)
    await trial("tamper", "tamper", 0, script=TAMPER)
    if bad("tamper", 0):
        return repair("tamper")
    await trial(
        "challenge", "independent_challenge", 0, script=critic.challenge.script, oracle=True
    )
    if bad("challenge", 0):
        return repair("challenge")
    for i, model in enumerate(config.solver_models):
        name = f"solver-{i}"
        await trial(name, "solver", None, model=model)
        if bad(name, None):
            return repair(name)
    await trial("adversary", "adversary", None, model=config.solver_models[0])
    if bad("adversary", None):
        return repair("adversary")

    # Enrich this dossier with the controller's paired proof. Immutable per-trial
    # outcome.json files retain the original provisional observation.
    for outcome in outcomes.values():
        outcome.materialization_verified = all(
            getattr(outcome, key) == witness[key]
            for key in ("task_digest", "profile_digest", "materialization_digest", "provider")
        )

    texts = {
        "instruction": instruction,
        "contract": bundle.model_dump_json(indent=2),
        "protected_tests": tests + "\nSOURCE OBSERVATION PROBE\n" + value.source_origin_probe,
        "oracle": (task / "solution/solve.sh").read_text()
        + "\n"
        + _trial_text(outcomes["oracle-0"])
        + "\n"
        + _trial_text(outcomes["oracle-1"]),
        "environment": "\n".join(
            (task / p).read_text()
            for p in (
                "task.toml",
                "environment/Dockerfile",
                "tests/Dockerfile",
                "tests/runner.py",
                "tests/probe.py",
            )
        ),
        "controls": "\n".join(
            _trial_text(v)
            for k, v in outcomes.items()
            if k not in {"oracle-0", "oracle-1", "solver-0", "solver-1", "adversary"}
        )
        + "\n"
        + value.contract.model_dump_json(indent=2),
        "solver_0": _trial_text(outcomes["solver-0"], trace=True),
        "solver_1": _trial_text(outcomes["solver-1"], trace=True),
        "adversary": _trial_text(outcomes["adversary"], trace=True),
        "submissions": submission_changes(outcomes["baseline"], outcomes),
        "bootstrap": json.dumps(construction["readiness"]),
        "comprehension": comprehension.model_dump_json(indent=2),
        "critic": critic.model_dump_json(indent=2),
        "source_witness": json.dumps(witness, indent=2),
    }
    for name, text in texts.items():
        evidence(name, text)
    records = []
    for name, outcome in outcomes.items():
        evidence_id = "trial_" + name
        evidence(evidence_id, _trial_text(outcome))
        details = json.loads((Path(outcome.path) / "verifier/details.json").read_text())
        matches = all(
            getattr(outcome, k) == witness[k]
            for k in ("task_digest", "profile_digest", "materialization_digest", "provider")
        )
        healthy = (
            outcome.reward in (0, 1)
            and not outcome.error
            and outcome.collection_verified
            and outcome.cleanup_confirmed
            and matches
        )
        records.append(
            TrialRecord(
                id=name,
                revision_digest=revision_digest,
                kind=outcome.kind,
                outcome=("passed" if outcome.reward == 1 else "submission_failure")
                if healthy
                else "incomplete",
                reward=int(outcome.reward) if healthy else None,
                environment_healthy=healthy,
                collection_complete=outcome.collection_verified,
                materialization_verified=matches,
                checks_run=details.get("n_tests", 0),
                evidence_ids=[evidence_id, "source_witness"],
                diagnostic=outcome.error
                or "Protected result and exact transferred source verified",
                model=outcome.model,
            )
        )
    report = await final_review(
        config=config,
        budget=budget,
        root=root / "final-review",
        deadline=deadline,
        pr=PRIdentity.from_url(source["url"]),
        revision_digest=revision_digest,
        evidence=texts,
    )
    context = AdmissionContext(
        pr=PRIdentity.from_url(source["url"]),
        revision_digest=revision_digest,
        required_trials=required,
        trials=records,
        evidence=evidence_refs,
        execution_profile_validated=witness["passed"] and outcomes["oracle-1"].reward == 1,
        artifact_integrity_verified=digest_task(task) == revision_digest,
    )
    reasons = admission_reasons(report, context)
    save_json(root / "admission-context.json", context.model_dump(mode="json"))
    save_json(root / "quality-report.json", report.model_dump(mode="json"))
    if reasons:
        return {
            "status": "needs_repair",
            "repairable": True,
            "stage": "final_review",
            "reasons": reasons,
            "review": report.model_dump(mode="json"),
        }
    journal.select_revision(lease, report=report, context=context)
    result = {
        "status": "accepted",
        "repairable": False,
        "task_path": str(task),
        "task_digest": revision_digest,
        "quality_report": str(root / "quality-report.json"),
        "admission_context": str(root / "admission-context.json"),
    }
    save_json(root / "accepted.json", result)
    return result
