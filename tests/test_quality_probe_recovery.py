"""A failed mutation is repairable; a completed counterexample is permanent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.artifacts import digest, probe_variant, task_identity
from repo2rlenv.quality.loop.models import (
    Assessment,
    Citation,
    Edit,
    Issue,
    LoopOptions,
    ProbeManifest,
    Repair,
    Review,
    SemanticProbe,
    TrialRecord,
)
from repo2rlenv.quality.loop.probe_recovery import (
    capture_attempt,
    failed_installations,
    record_attempt,
    replacement_evidence,
)
from repo2rlenv.quality.loop.remote import RemoteTrials
from repo2rlenv.quality.loop.runner import QualityLoop


@pytest.fixture
def task(tmp_path):
    return write_bundle(
        TaskBundle(
            name="probe-recovery",
            org="tests",
            instruction="Add two integers.\n",
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\n# check arithmetic\n", executable=True),
            },
        ),
        tmp_path / "tasks",
    )


def probes():
    return [
        SemanticProbe(
            name=name,
            kind=kind,
            rationale="Check the arithmetic contract",
            evidence=[Citation(path="instruction.md", quote="Add two integers.")],
            script=script,
        )
        for name, kind, script in [
            ("wrong-sum", "wrong_solution", "# original mutation"),
            ("valid-sum", "valid_alternative", "# distinct valid implementation"),
        ]
    ]


def review(*, proposal=False, diagnosis=False, rollout=False, category="probe"):
    assessment = Assessment(
        status="pass",
        score=3,
        explanation="A behavioral task.",
        evidence=[Citation(path="instruction.md", quote="Add two integers.")],
    )
    return Review(
        summary="Review the task and the actual probe installation.",
        task=assessment,
        verifier=assessment,
        leakage=assessment,
        rollout="legitimate_success" if rollout else "not_run",
        probes=probes() if proposal else [],
        read_requests=[],
        issues=[
            Issue(
                category=category,
                severity="blocking",
                problem="The wrong-sum mutation changed nothing.",
                repair="Correct the exact source match in the mutation.",
                evidence=[
                    Citation(
                        path="evidence/2-probe/agent/oracle.txt",
                        quote="Semantic probe left the collected submission unchanged",
                    )
                ],
            )
        ]
        if diagnosis
        else [],
    )


class Trials:
    """Materialize receipts only; never execute task code or make provider calls."""

    def __init__(self, root):
        self.root, self.calls = root, []

    def run(self, task, role, key):
        self.calls.append(key)
        failed = key == "r0-probe0"
        reward = float(
            role != "baseline" and not (role == "probe" and "probe0" in key and not failed)
        )
        agent = (
            "oracle"
            if role in {"oracle", "probe"}
            else "nop"
            if role == "baseline"
            else "terminus-2"
        )
        trial_id = "test-" + key
        path = self.root / key / trial_id / task.name / "result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "config": {
                        "task": {"path": str(task)},
                        "agent": {"name": agent, "model_name": None},
                    },
                    "verifier_result": {"rewards": {"reward": reward}},
                    "exception_info": None,
                }
            )
        )
        logs = path.parent / "agent"
        logs.mkdir()
        (logs / "exit-code.txt").write_text("1" if failed else "0")
        if role == "probe":
            (logs / "oracle.txt").write_text(
                "ValueError: Semantic probe left the collected submission unchanged\n"
                if failed
                else "__QUALITY_PROBE_COMPLETED__\n"
            )
        (self.root / key / "trial.json").write_text(
            json.dumps(
                {
                    "state": "completed",
                    "trial_id": trial_id,
                    "bundle_hash": task_identity(task),
                    "result_sha256": digest(path),
                }
            )
        )
        return TrialRecord(
            role=role,
            bundle_hash=task_identity(task),
            result=str(path),
            result_sha256=digest(path),
            agent=agent,
            model=None,
            reward=reward,
            exception_type=None,
            binding="receipt",
            agent_exit_code=1 if failed else 0,
            probe_installed=not failed if role == "probe" else None,
        )

    def close(self):
        pass


class Model:
    def __init__(self, *, diagnose=True, invalid_patch=False):
        self.calls, self.diagnose, self.invalid_patch = [], diagnose, invalid_patch

    def ask(self, schema, model, system, user, key):
        payload = json.loads(user)
        self.calls.append((key, payload))
        if schema is Repair:
            corrected = probes()[0].model_copy(update={"script": "# corrected mutation"})
            return Repair(
                explanation="Fix the no-op mutation without changing the task.",
                addressed_issues=["The wrong-sum mutation changed nothing."],
                edits=[],
                probe_replacements=[probes()[0] if self.invalid_patch else corrected],
            )
        # Reproduce PEFT2952: the first post-probe review overlooks the failure.
        return review(
            proposal=payload["probe_limit"] > 0,
            diagnosis=self.diagnose and key.startswith("r0-after-") and not key.endswith("-0"),
            rollout=any("rollout/result.json" in path for path in payload["documents"]),
        )


def loop(tmp_path, *, model=None, remote=None, **options):
    return QualityLoop(
        LoopOptions(repair=True, max_repairs=1, max_read_rounds=1, **options),
        tmp_path / "quality",
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10"),
        model_client=model or Model(),
        trial_runner=remote,
    )


def test_ignored_noop_probe_gets_bounded_diagnosis_and_only_its_control_reruns(task, tmp_path):
    remote, model = Trials(tmp_path / "quality/trials"), Model()
    result = loop(tmp_path, remote=remote, model=model).run(task)
    assert result.status == "usable", result.reasons
    assert result.repairs == 1 and result.bundle_hash == task_identity(task)
    assert [key for key in remote.calls if key.startswith("r1")] == ["r1-probe0", "r1-rollout"]
    correction = next(payload for key, payload in model.calls if key == "r0-after-1")
    assert "category='probe'" in correction["protocol_feedback"][0]
    assert correction["review_calls_remaining"] == 0
    assert {
        p["name"]
        for p in next(payload for key, payload in model.calls if key == "r0-repair")[
            "probe_replacement_policy"
        ]["allowed_replacements"]
    } == {"wrong-sum", "valid-sum"}
    authorization = tmp_path / "quality/revisions/r1/probe-replacement-evidence.json"
    assert (
        json.loads(authorization.read_text())["replacements"]["wrong-sum"]["attempts"][0]["trial"][
            "agent_exit_code"
        ]
        == 1
    )
    assert (tmp_path / "quality/probe-attempts/r0-probe0.json").exists()
    assert (tmp_path / "quality/probes/r0-probe0/probe.json").exists()
    assert any(
        item.probe and item.probe.name == "valid-sum" and "/r0-probe1/" in item.result
        for item in result.trials
    )


@pytest.mark.parametrize("diagnose,invalid_patch", [(False, False), (True, True)])
def test_unresolved_diagnosis_or_proposal_stops_with_existing_call_limits(
    task, tmp_path, diagnose, invalid_patch
):
    model, remote = (
        Model(diagnose=diagnose, invalid_patch=invalid_patch),
        Trials(tmp_path / "quality/trials"),
    )
    result = loop(tmp_path, model=model, remote=remote).run(task)
    assert result.status == "needs_evidence"
    repairs = [key for key, _ in model.calls if "-repair" in key]
    assert repairs == (["r0-repair", "r0-repair-correction1"] if diagnose else [])
    assert len([key for key, _ in model.calls if key.startswith("r0-after-")]) == 2
    assert not any(key.startswith("r1") for key in remote.calls)
    assert task_identity(task) == result.bundle_hash


def failed_attempt(task, tmp_path):
    variant = probe_variant(task, probes()[0], tmp_path / "variants/current" / task.name)
    trial = Trials(tmp_path / "trials").run(variant, "probe", "r0-probe0")
    trial.probe = probes()[0]
    return trial, capture_attempt(task_identity(task), trial)


def eligibility(task, tmp_path, trial, attempts, *, diagnosed=True, category="probe"):
    runner = loop(tmp_path)
    # Prefix index 2 agrees with actual control -> probe ordering.
    placeholder = trial.model_copy(
        update={"role": "oracle", "bundle_hash": task_identity(task), "probe": None}
    )
    trials = [placeholder, placeholder, trial]
    failures = failed_installations(task, trials)
    context = runner._context(task, trials, uninstalled_probes=failures)
    return replacement_evidence(
        task,
        probes()[0],
        attempts,
        failures,
        review(diagnosis=diagnosed, category=category),
        context,
    )


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "missing_receipt",
        "result",
        "summary",
        "log",
        "exit",
        "manifest",
        "parent",
        "no_history",
        "checksum_only",
        "zero_exit",
    ],
)
def test_only_independently_bound_failed_installation_can_authorize(task, tmp_path, change):
    trial, attempt = failed_attempt(task, tmp_path)
    result = Path(trial.result)
    if change == "missing_receipt":
        (result.parents[2] / "trial.json").unlink()
    elif change == "result":
        result.write_text(result.read_text() + " ")
    elif change == "summary":
        trial.agent_exit_code = 0
    elif change == "log":
        (result.parent / "agent/oracle.txt").write_text("__QUALITY_PROBE_COMPLETED__\n")
    elif change == "exit":
        (result.parent / "agent/exit-code.txt").unlink()
    elif change == "manifest":
        (tmp_path / "variants/current/probe.json").write_text("{}")
    elif change == "parent":
        attempt["parent_hash"] = "sha256:" + "0" * 64
    elif change == "checksum_only":
        trial.binding = "harbor_checksum"
        attempt = capture_attempt(task_identity(task), trial)
    elif change == "zero_exit":
        trial.agent_exit_code = 0
        (result.parent / "agent/exit-code.txt").write_text("0")
        attempt = capture_attempt(task_identity(task), trial)
    if change == "result":
        # EvidenceContext itself rejects this mismatch before a model is invoked.
        with pytest.raises(ValueError, match="changed since ingestion"):
            eligibility(task, tmp_path, trial, [attempt])
    else:
        value = eligibility(task, tmp_path, trial, [] if change == "no_history" else [attempt])
        assert (value is not None) == (change == "none")


@pytest.mark.parametrize("diagnosed,category", [(False, "probe"), (True, "verifier")])
def test_wrong_probe_correction_requires_its_grounded_probe_diagnosis(
    task, tmp_path, diagnosed, category
):
    trial, attempt = failed_attempt(task, tmp_path)
    assert (
        eligibility(task, tmp_path, trial, [attempt], diagnosed=diagnosed, category=category)
        is None
    )


@pytest.mark.parametrize("different_script,reward", [(False, 0.0), (False, 1.0), (True, 1.0)])
def test_any_earlier_completed_same_name_remains_immutable_after_later_failure(
    task, tmp_path, different_script, reward
):
    trial, failed = failed_attempt(task, tmp_path)
    old = (
        probes()[0].model_copy(update={"script": "# older script"})
        if different_script
        else probes()[0]
    )
    variant = probe_variant(task, old, tmp_path / "variants/earlier" / task.name)
    installed = Trials(tmp_path / "older-trials").run(variant, "probe", "prior-probe")
    installed.probe = old
    # Both a rejected wrong solution and a verifier-gap counterexample are permanent.
    path = Path(installed.result)
    raw = json.loads(path.read_text())
    raw["verifier_result"]["rewards"]["reward"] = reward
    path.write_text(json.dumps(raw))
    installed.reward, installed.result_sha256 = reward, digest(path)
    receipt = path.parents[2] / "trial.json"
    receipt.write_text(
        json.dumps({**json.loads(receipt.read_text()), "result_sha256": installed.result_sha256})
    )
    prior = capture_attempt(task_identity(task), installed)
    assert eligibility(task, tmp_path, trial, [prior, failed]) is None


def test_attempt_journal_refuses_rewritten_installation_on_resume(task, tmp_path):
    trial, _ = failed_attempt(task, tmp_path)
    record_attempt(tmp_path / "quality", "r0-probe0", task_identity(task), trial)
    (Path(trial.result).parent / "agent/oracle.txt").write_text("different failure\n")
    with pytest.raises(ValueError, match="Preserved probe installation evidence changed"):
        record_attempt(tmp_path / "quality", "r0-probe0", task_identity(task), trial)


def test_noop_probe_alone_cannot_authorize_task_edits(task, tmp_path):
    class TaskEditModel(Model):
        def ask(self, schema, *args):
            response = super().ask(schema, *args)
            if schema is Repair:
                response.edits = [
                    Edit(
                        path="tests/test.sh",
                        old="check arithmetic",
                        new="reject no-op",
                        executable=True,
                    )
                ]
            return response

    model = TaskEditModel()
    result = loop(tmp_path, model=model, remote=Trials(tmp_path / "quality/trials")).run(task)
    assert result.status == "needs_evidence"
    assert "does not justify task/verifier edits" in result.reasons[0]
    assert result.bundle_hash == task_identity(task)
    assert len([key for key, _ in model.calls if "-repair" in key]) == 2


def test_one_invalid_wrong_probe_proposal_can_use_the_existing_correction(task, tmp_path):
    class CorrectingModel(Model):
        def ask(self, schema, *args):
            response = super().ask(schema, *args)
            if schema is Repair and len([key for key, _ in self.calls if "-repair" in key]) == 1:
                response.probe_replacements = [probes()[0]]
            return response

    model = CorrectingModel()
    result = loop(tmp_path, model=model, remote=Trials(tmp_path / "quality/trials")).run(task)
    assert result.status == "usable", result.reasons
    patches = [(key, payload) for key, payload in model.calls if "-repair" in key]
    assert [key for key, _ in patches] == ["r0-repair", "r0-repair-correction1"]
    assert patches[1][1]["correction_calls_remaining"] == 0
    assert "must change its script" in patches[1][1]["patch_feedback"][0]


def test_evidence_changed_while_model_authors_patch_denies_authorization(task, tmp_path):
    class ChangedEvidenceModel(Model):
        def ask(self, schema, *args):
            response = super().ask(schema, *args)
            if schema is Repair:
                trial = json.loads(
                    (tmp_path / "quality/probe-attempts/r0-probe0.json").read_text()
                )["trial"]
                (Path(trial["result"]).parent / "agent/oracle.txt").write_text("new failure\n")
            return response

    result = loop(
        tmp_path, model=ChangedEvidenceModel(), remote=Trials(tmp_path / "quality/trials")
    ).run(task)
    assert result.status == "needs_evidence"
    assert "evidence changed during repair" in result.reasons[0]
    assert not (tmp_path / "quality/revisions/r1").exists()


def test_completed_probe_in_different_task_revision_still_blocks_replacement(task, tmp_path):
    from repo2rlenv.quality.loop.artifacts import apply_repair

    trial, failed = failed_attempt(task, tmp_path)
    older = apply_repair(
        task,
        Repair(
            explanation="An older verifier revision.",
            addressed_issues=["check"],
            edits=[
                Edit(
                    path="tests/test.sh", old="check arithmetic", new="older check", executable=True
                )
            ],
        ),
        tmp_path / "older" / task.name,
    )
    variant = probe_variant(older, probes()[0], tmp_path / "variants/older" / task.name)
    installed = Trials(tmp_path / "older-trials").run(variant, "probe", "prior-probe")
    installed.probe = probes()[0]
    prior = capture_attempt(task_identity(older), installed)
    assert prior["parent_hash"] != failed["parent_hash"]
    assert eligibility(task, tmp_path, trial, [prior, failed]) is None


def test_resume_replays_original_authorization_before_later_completed_probe(task, tmp_path):
    class CachedTrials(Trials):
        def run(self, task, role, key):
            if (self.root / key / "trial.json").exists():
                return RemoteTrials._read(self.root / key, task, role)
            return super().run(task, role, key)

    remote = CachedTrials(tmp_path / "quality/trials")
    runner = loop(tmp_path, remote=remote)
    first = runner.run(task)
    assert first.status == "usable", first.reasons
    files = {
        str(path): digest(path) for path in (tmp_path / "quality/probe-attempts").glob("*.json")
    }
    calls = list(remote.calls)
    resumed = runner.run(task, resume=True)
    assert resumed.status == "usable", resumed.reasons
    assert remote.calls == calls
    assert files == {
        str(path): digest(path) for path in (tmp_path / "quality/probe-attempts").glob("*.json")
    }


def test_imported_wrong_probe_cannot_lose_unknown_prior_counterexample_history(task, tmp_path):
    manifest = tmp_path / "known-probes.json"
    manifest.write_text(
        ProbeManifest(bundle_hash=task_identity(task), probes=probes()).model_dump_json()
    )
    remote, model = Trials(tmp_path / "quality/trials"), Model()
    result = loop(tmp_path, remote=remote, model=model).run(task, probes=manifest)
    assert result.status == "needs_evidence"
    assert "wrong-solution probes cannot be replaced" in result.reasons[0]
    proposals = [payload for key, payload in model.calls if "-repair" in key]
    assert len(proposals) == 2
    assert all(
        "wrong-sum" in payload["probe_replacement_policy"]["immutable_wrong_solution_probes"]
        for payload in proposals
    )
    assert not any(key.startswith("r1") for key in remote.calls)
