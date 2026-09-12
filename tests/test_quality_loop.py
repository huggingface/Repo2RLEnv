from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("harbor")

from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.artifacts import (
    apply_repair,
    digest,
    import_trial,
    probe_variant,
    refresh_identity,
    task_identity,
)
from repo2rlenv.quality.loop.client import JsonModel, ModelRequestError, RunBudget, response_schema
from repo2rlenv.quality.loop.context import EvidenceContext
from repo2rlenv.quality.loop.models import (
    Assessment,
    Citation,
    Edit,
    Issue,
    LoopOptions,
    ReadRequest,
    Repair,
    Review,
    SemanticProbe,
    TrialRecord,
)
from repo2rlenv.quality.loop.runner import (
    QualityLoop,
    control_failures,
    probe_failures,
    required_probe_focus,
)


@pytest.fixture
def task(tmp_path):
    return write_bundle(
        TaskBundle(
            name="example",
            org="tests",
            instruction="Write the sum of two integers to answer.txt.\n",
            files={
                "environment/Dockerfile": TaskFile.text(
                    "FROM python:3.12-slim\nWORKDIR /workspace\n"
                ),
                "solution/solve.sh": TaskFile.text(
                    "#!/bin/sh\nprintf '4' > /workspace/answer.txt\n", executable=True
                ),
                "tests/test.sh": TaskFile.text("#!/bin/sh\n# weak check\n", executable=True),
            },
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
        ),
        tmp_path / "tasks",
    )


def probes():
    evidence = [Citation(path="instruction.md", quote="sum of two integers")]
    return [
        SemanticProbe(
            name="wrong-sum",
            kind="wrong_solution",
            rationale="Wrong arithmetic",
            evidence=evidence,
            script="printf '7' > /workspace/answer.txt",
        ),
        SemanticProbe(
            name="valid-format",
            kind="valid_alternative",
            rationale="Trailing newline is valid",
            evidence=evidence,
            script="printf '4\\n' > /workspace/answer.txt",
        ),
    ]


def review(*, broken=False, propose=False, rollout="not_run"):
    assessment = Assessment(
        status="pass",
        score=3,
        explanation="Useful behavioral task",
        evidence=[Citation(path="instruction.md", quote="sum of two integers")],
    )
    return Review(
        summary="Check an arithmetic task",
        task=assessment,
        leakage=assessment,
        verifier=assessment.model_copy(update={"status": "fail" if broken else "pass"}),
        rollout=rollout,
        issues=[
            Issue(
                category="verifier",
                severity="blocking",
                problem="Weak arithmetic check",
                repair="Exercise the sum",
                evidence=[Citation(path="tests/test.sh", quote="weak check")],
            )
        ]
        if broken
        else [],
        probes=probes() if propose else [],
        read_requests=[],
    )


class Model:
    def __init__(self, *, honest=True, solver_failure=False):
        self.calls, self.honest, self.solver_failure = [], honest, solver_failure

    def ask(self, schema, model, system, user, key):
        self.calls.append(key)
        if schema is Repair:
            return Repair(
                explanation="Repair arithmetic coverage",
                addressed_issues=["Weak check"],
                edits=[
                    Edit(
                        path="tests/test.sh", old="weak check", new="strong check", executable=True
                    )
                ],
            )
        payload = json.loads(user)
        broken = self.honest and "weak check" in payload["documents"]["tests/test.sh"]
        rolled = any("rollout/result.json" in path for path in payload["documents"])
        outcome = "legitimate_failure" if self.solver_failure else "legitimate_success"
        return review(
            broken=broken,
            propose=payload["probe_limit"] > 0,
            rollout=outcome if rolled else "not_run",
        )


class Trials:
    def __init__(self, root, *, solver_failure=False):
        self.root, self.calls, self.closed = root, [], False
        self.solver_failure = solver_failure

    def run(self, task, role, key):
        self.calls.append((role, key, task_identity(task)))
        path = self.root / key / "result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        reward = 0.0 if role == "baseline" or (role == "rollout" and self.solver_failure) else 1.0
        if (
            role == "probe"
            and "probe0" in key
            and "strong check" in (task / "tests/test.sh").read_text()
        ):
            reward = 0.0
        return TrialRecord(
            role=role,
            bundle_hash=task_identity(task),
            result=str(path),
            result_sha256=digest(path),
            agent=role,
            model=None,
            reward=reward,
            exception_type=None,
            binding="receipt",
            probe_installed=role == "probe",
        )

    def close(self):
        self.closed = True


def make_loop(tmp_path, *, model=None, remote=None, **options):
    return QualityLoop(
        LoopOptions(**options),
        tmp_path / "quality",
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="20"),
        model_client=model or Model(),
        trial_runner=remote,
    )


def test_repairs_copy_and_revalidate_everything(task, tmp_path):
    before = task_identity(task)
    remote = Trials(tmp_path / "evidence")
    loop = make_loop(tmp_path, remote=remote, repair=True)
    result = loop.run(task)
    assert result.status == "usable", result.reasons
    assert result.repairs == 1
    assert task_identity(task) == before != result.bundle_hash
    assert len(remote.calls) == 9
    assert [role for role, key, _ in remote.calls if key.startswith("r1")] == [
        "baseline",
        "oracle",
        "probe",
        "probe",
        "rollout",
    ]
    assert remote.closed


def test_false_positive_cannot_be_outvoted_by_model(task, tmp_path):
    result = make_loop(
        tmp_path, remote=Trials(tmp_path / "evidence"), model=Model(honest=False), run_rollout=True
    ).run(task)
    assert result.status == "needs_repair"
    assert any("wrong_solution earned 1" in reason for reason in result.reasons)


def test_legitimate_solver_failure_keeps_a_sound_task(task, tmp_path):
    remote = Trials(tmp_path / "evidence", solver_failure=True)
    result = make_loop(tmp_path, remote=remote, model=Model(solver_failure=True), repair=True).run(
        task
    )
    assert result.status == "usable"
    assert next(trial for trial in result.trials if trial.role == "rollout").reward == 0


def test_review_only_is_never_validated(task, tmp_path):
    result = make_loop(tmp_path, model=Model(honest=False)).run(task)
    assert result.status == "reviewed"
    assert result.trials == []
    assert any("Missing" in reason for reason in result.reasons)


def test_max_repairs_keeps_bad_task_unselected(task, tmp_path):
    result = make_loop(
        tmp_path, remote=Trials(tmp_path / "evidence"), repair=True, max_repairs=0
    ).run(task)
    assert result.status == "needs_repair"
    assert result.repairs == 0


def test_invalid_patch_gets_one_correction_without_an_extra_execution_revision(task, tmp_path):
    class Correcting(Model):
        def ask(self, schema, model, system, user, key):
            result = super().ask(schema, model, system, user, key)
            if schema is Repair:
                payload = json.loads(user)
                if not payload["patch_feedback"]:
                    result.edits[0].old = "weak check with invented surrounding code"
                else:
                    assert "exactly once" in payload["patch_feedback"][0]
            return result

    model = Correcting()
    remote = Trials(tmp_path / "results")
    result = make_loop(tmp_path, model=model, remote=remote, repair=True).run(task)
    assert result.status == "usable"
    assert result.repairs == 1
    assert len([key for key in model.calls if "repair" in key]) == 2
    assert len(remote.calls) == 9


def test_repeated_invalid_patch_stops_without_changing_task(task, tmp_path):
    class Invalid(Model):
        def ask(self, schema, *args):
            result = super().ask(schema, *args)
            if schema is Repair:
                result.edits[0].old = "invented text"
            return result

    model = Invalid()
    result = make_loop(tmp_path, model=model, repair=True).run(task)
    assert result.status == "needs_evidence"
    assert result.repairs == 0
    assert len([key for key in model.calls if "repair" in key]) == 2
    assert result.bundle_hash == task_identity(task)


@pytest.mark.parametrize(
    "path", ["../escape", "/tmp/escape", "environment/../../escape", "task.toml"]
)
def test_repair_cannot_escape_or_rewrite_execution_policy(task, tmp_path, path):
    patch = Repair(
        explanation="bad edit",
        addressed_issues=["test"],
        edits=[Edit(path=path, old="", new="x", executable=False)],
    )
    with pytest.raises(ValueError):
        apply_repair(task, patch, tmp_path / "revision")
    assert not (tmp_path / "revision").exists()


def test_ambiguous_edit_is_atomic(task, tmp_path):
    patch = Repair(
        explanation="bad match",
        addressed_issues=["test"],
        edits=[Edit(path="instruction.md", old="not found", new="changed", executable=False)],
    )
    with pytest.raises(ValueError, match="exactly once"):
        apply_repair(task, patch, tmp_path / "revision")
    assert not (tmp_path / "revision").exists()


def test_probe_keeps_task_and_verifier_bytes(task, tmp_path):
    revised = probe_variant(task, probes()[0], tmp_path / "probe" / task.name)
    for file in ["instruction.md", "tests/test.sh", "environment/Dockerfile"]:
        assert (task / file).read_bytes() == (revised / file).read_bytes()
    assert "__QUALITY_PROBE_COMPLETED__" in (revised / "solution/solve.sh").read_text()
    assert (revised / "solution/quality-original-solve.sh").read_bytes() == (
        task / "solution/solve.sh"
    ).read_bytes()


def test_fabricated_citation_is_rejected(task):
    context = EvidenceContext(task, [], limit=16000)
    value = review()
    value.task.evidence[0].quote = "invented evidence"
    with pytest.raises(ValueError, match="not grounded"):
        context.validate_review(value)


def raw_trial(task, tmp_path, *, agent="terminus-2"):
    from harbor.models.task.task import Task

    path = tmp_path / "raw" / "result.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "task_checksum": Task(task).checksum,
                "config": {"agent": {"name": agent, "model_name": "test"}},
                "verifier_result": {"rewards": {"reward": 1}},
            }
        )
    )
    return path


def test_native_harbor_rollout_is_bound_by_checksum(task, tmp_path):
    path = raw_trial(task, tmp_path)
    evidence = import_trial(path, task, "rollout")
    assert evidence.binding == "harbor_checksum"
    data = json.loads(path.read_text())
    data["task_checksum"] = "0" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="checksum"):
        import_trial(path, task, "rollout")


def test_oracle_cannot_be_imported_as_blind_solver(task, tmp_path):
    path = raw_trial(task, tmp_path, agent="oracle")
    with pytest.raises(ValueError, match="solver"):
        import_trial(path, task, "rollout")


def test_receipt_cannot_redirect_to_another_directory(task, tmp_path):
    receipt = tmp_path / "trial.json"
    receipt.write_text(
        json.dumps(
            {
                "state": "completed",
                "bundle_hash": task_identity(task),
                "trial_id": "../outside",
            }
        )
    )
    with pytest.raises(ValueError, match="invalid trial ID"):
        import_trial(receipt, task, "rollout")


def test_oracle_exit_failure_and_probe_install_failure_are_not_valid_controls(task, tmp_path):
    item = Trials(tmp_path / "results").run(task, "oracle", "one")
    assert "oracle did not execute cleanly" in control_failures(
        [item.model_copy(update={"agent_exit_code": 1})], 1
    )
    item = item.model_copy(
        update={"role": "probe", "probe": probes()[0], "reward": 0, "probe_installed": False}
    )
    assert "install/execute cleanly" in probe_failures([item], 1)[0]


def test_run_budget_preserves_campaign_and_uncertain_holds(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10")
    budget = RunBudget(ledger, "quality-example", "2")
    budget.reserve("quality-example:one", "1.50", "review")
    budget.mark_uncertain("quality-example:one", "provider timed out")
    with pytest.raises(BudgetExceeded):
        budget.reserve("quality-example:two", "1", "review")
    assert ledger.status()["reserved_usd"] == "1.500000"


def test_changed_options_cannot_resume(task, tmp_path):
    make_loop(tmp_path).run(task)
    with pytest.raises(ValueError, match="identical"):
        make_loop(tmp_path, max_repairs=1).run(task, resume=True)


def test_boolean_reward_is_invalid(task, tmp_path):
    path = raw_trial(task, tmp_path)
    data = json.loads(path.read_text())
    data["verifier_result"]["rewards"]["reward"] = True
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        import_trial(path, task, "rollout")


def test_completed_model_response_is_reused_without_another_paid_call(tmp_path, monkeypatch):
    from repo2rlenv.llm import LLMResponse

    calls = []
    monkeypatch.setattr("repo2rlenv.quality.loop.client.resolve_llm_api_key", lambda *args: "test")

    def complete(*args, **kwargs):
        calls.append(kwargs)
        return LLMResponse(
            content=review().model_dump_json(), usage={"prompt_tokens": 10}, cost_usd=0.01
        )

    monkeypatch.setattr("repo2rlenv.campaigns.llm.complete", complete)
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10")
    client = JsonModel(
        tmp_path / "calls", RunBudget(ledger, "quality-test", "2"), reservation="1", max_tokens=2000
    )
    args = (Review, LoopOptions().review_model, "system", "user", "review")
    assert client.ask(*args) == client.ask(*args)
    assert len(calls) == 1
    assert ledger.status()["accounted_usd"] == "0.010000"
    with pytest.raises(ModelRequestError, match="request failed"):
        client.ask(Review, LoopOptions().review_model, "system", "changed user", "review")
    assert len(calls) == 1


def test_uncertain_model_request_is_never_dispatched_again(tmp_path, monkeypatch):
    monkeypatch.setattr("repo2rlenv.quality.loop.client.resolve_llm_api_key", lambda *args: "test")
    calls = []

    def complete(*args, **kwargs):
        calls.append(kwargs)
        raise TimeoutError("uncertain provider response")

    monkeypatch.setattr("repo2rlenv.campaigns.llm.complete", complete)
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10")
    client = JsonModel(
        tmp_path / "calls", RunBudget(ledger, "quality-test", "2"), reservation="1", max_tokens=2000
    )
    for _ in range(2):
        with pytest.raises(ModelRequestError):
            client.ask(Review, LoopOptions().review_model, "system", "user", "review")
    assert len(calls) == 1
    assert ledger.status()["reserved_usd"] == "1.000000"


def test_missing_credentials_do_not_reserve_money(tmp_path, monkeypatch):
    monkeypatch.setattr("repo2rlenv.quality.loop.client.resolve_llm_api_key", lambda *args: None)
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10")
    client = JsonModel(
        tmp_path / "calls", RunBudget(ledger, "quality-test", "2"), reservation="1", max_tokens=2000
    )
    with pytest.raises(ValueError, match="No API key"):
        client.ask(Review, LoopOptions().review_model, "system", "user", "review")
    assert ledger.status()["operations"] == []


def test_fake_citations_cannot_trigger_a_repair(task, tmp_path):
    class Ungrounded(Model):
        def ask(self, *args):
            value = super().ask(*args)
            value.task.evidence[0].quote = "invented requirement"
            return value

    model = Ungrounded()
    result = make_loop(tmp_path, model=model, max_read_rounds=0, repair=True).run(task)
    assert result.status == "needs_evidence"
    assert result.repairs == 0
    assert len(model.calls) == 1


def test_trial_artifact_symlink_cannot_escape_evidence_root(task, tmp_path):
    item = Trials(tmp_path / "results").run(task, "rollout", "one")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "trajectory.json").write_text("private material")
    (Path(item.result).parent / "agent").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        EvidenceContext(task, [item], limit=16000)


def test_bounded_search_finds_relevant_code_without_loading_whole_repository(task):
    context = EvidenceContext(task, [], limit=16000)
    context.read_more(
        [ReadRequest(path="solution/solve.sh", query="printf", start_line=1, end_line=1)]
    )
    assert "printf" in context.documents["solution/solve.sh:search=printf"]
    with pytest.raises(ValueError, match="inventory"):
        context.read_more([ReadRequest(path="../../secret", query=None, start_line=1, end_line=2)])


def test_prior_revision_controls_cannot_validate_current_task(task, tmp_path):
    evidence = Trials(tmp_path / "results").run(task, "oracle", "one")
    evidence.bundle_hash = "sha256:" + "0" * 64
    loop = make_loop(tmp_path)
    with pytest.raises(ValueError, match="another task revision"):
        loop._context(task, [evidence])


def test_generic_probes_cannot_cover_explicit_laziness(task, tmp_path):
    patch = Repair(
        explanation="Add an output contract",
        addressed_issues=["contract"],
        edits=[
            Edit(
                path="instruction.md",
                old="Write the sum",
                new="Yield lazily. Write the sum",
                executable=False,
            )
        ],
    )
    revised = apply_repair(task, patch, tmp_path / "input" / task.name)
    assert required_probe_focus(revised) == {"lazy_output"}
    result = make_loop(
        tmp_path,
        remote=Trials(tmp_path / "results"),
        model=Model(honest=False),
        run_rollout=True,
        max_read_rounds=0,
    ).run(revised)
    assert result.status == "needs_evidence"
    assert not any(item.role in {"rollout", "probe"} for item in result.trials)


def test_generator_input_does_not_imply_lazy_output(task, tmp_path):
    patch = Repair(
        explanation="Document inputs",
        addressed_issues=["contract"],
        edits=[
            Edit(
                path="instruction.md",
                old="two integers",
                new="integers supplied by generators",
                executable=False,
            )
        ],
    )
    revised = apply_repair(task, patch, tmp_path / "input" / task.name)
    assert required_probe_focus(revised) == set()


def test_known_counterexamples_are_bound_and_replayed(task, tmp_path):
    path = tmp_path / "probes.json"
    path.write_text(
        json.dumps(
            {"bundle_hash": task_identity(task), "probes": [p.model_dump() for p in probes()]}
        )
    )
    result = make_loop(tmp_path, remote=Trials(tmp_path / "results"), repair=True).run(
        task, probes=path
    )
    assert result.status == "usable"
    assert result.repairs == 1
    path.write_text(
        json.dumps(
            {"bundle_hash": "sha256:" + "0" * 64, "probes": [p.model_dump() for p in probes()]}
        )
    )
    with pytest.raises(ValueError, match="match the task"):
        make_loop(tmp_path).run(task, probes=path)


def test_openai_schema_requires_focus_even_for_legacy_read_defaults():
    schema = response_schema(Review)
    assert "focus" in schema["$defs"]["SemanticProbe"]["required"]
    assert "default" not in schema["$defs"]["SemanticProbe"]["properties"]["focus"]


def test_wrong_solution_counterexample_cannot_be_replaced():
    with pytest.raises(ValueError, match="cannot be replaced"):
        Repair(
            explanation="Discard an inconvenient counterexample",
            addressed_issues=["probe"],
            edits=[],
            probe_replacements=[probes()[0]],
        )


class AlternativeRepairModel(Model):
    def __init__(self, *, category="probe", replacement=None):
        super().__init__()
        self.category = category
        self.replacement = replacement or probes()[1].model_copy(
            update={"script": "printf '4\\n' > /workspace/answer.txt # corrected"}
        )

    def ask(self, schema, model, system, user, key):
        self.calls.append(key)
        if schema is Repair:
            return Repair(
                explanation="Correct the alternative's diagnosed implementation bug",
                addressed_issues=["Invalid alternative implementation"],
                edits=[],
                probe_replacements=[self.replacement],
            )
        payload = json.loads(user)
        rolled = any("rollout/result.json" in path for path in payload["documents"])
        result = review(
            propose=payload["probe_limit"] > 0,
            rollout="legitimate_success" if rolled else "not_run",
        )
        if key.startswith("r0-after"):
            result.issues = [
                Issue(
                    category=self.category,
                    severity="blocking",
                    problem="Invalid alternative implementation",
                    repair="Correct the probe while preserving the grading contract",
                    evidence=result.task.evidence,
                )
            ]
        return result


class AlternativeTrials(Trials):
    def run(self, task, role, key):
        result = super().run(task, role, key)
        if role == "probe":
            correct_alternative = "corrected" in (task / "solution/solve.sh").read_text()
            result.reward = float("probe1" in key and correct_alternative)
        return result


def test_probe_only_repair_reuses_controls_and_preserves_counterexample(task, tmp_path):
    remote = AlternativeTrials(tmp_path / "results")
    result = make_loop(tmp_path, remote=remote, model=AlternativeRepairModel(), repair=True).run(
        task
    )
    assert result.status == "usable", result.reasons
    assert result.repairs == 1
    assert result.bundle_hash == task_identity(task)
    assert [role for role, key, _ in remote.calls if key.startswith("r1")] == [
        "probe",
        "probe",
        "rollout",
    ]
    wrong = next(
        item for item in result.trials if item.probe and item.probe.kind == "wrong_solution"
    )
    assert wrong.probe == probes()[0]
    assert wrong.reward == 0
    assert (tmp_path / "quality/revisions/r1/repair.json").is_file()


@pytest.mark.parametrize("defect", ["diagnosis", "name", "focus", "evidence"])
def test_alternative_repair_requires_grounding_and_stable_identity(task, tmp_path, defect):
    replacement = probes()[1]
    category = "verifier" if defect == "diagnosis" else "probe"
    updates = {
        "name": {"name": "unknown-probe"},
        "focus": {"focus": "lazy_output"},
        "evidence": {"evidence": [Citation(path="instruction.md", quote="invented contract")]},
    }
    replacement = replacement.model_copy(update=updates.get(defect, {}))
    result = make_loop(
        tmp_path,
        remote=AlternativeTrials(tmp_path / "results"),
        model=AlternativeRepairModel(category=category, replacement=replacement),
        repair=True,
    ).run(task)
    assert result.status == "needs_evidence"
    assert result.repairs == 0
    assert not (tmp_path / "quality/revisions/r1").exists()


def test_review_contains_actual_repository_test_failure(task, tmp_path):
    item = Trials(tmp_path / "results").run(task, "oracle", "one")
    verifier = Path(item.result).parent / "verifier"
    verifier.mkdir()
    (verifier / "stdout.txt").write_text("FAILED test_root_atomicity: actual != expected")
    context = EvidenceContext(task, [item], limit=16000)
    assert (
        "FAILED test_root_atomicity" in context.documents["evidence/0-oracle/verifier/stdout.txt"]
    )


def test_large_baseline_inventory_cannot_hide_later_probe_failure(task, tmp_path):
    remote = Trials(tmp_path / "results")
    baseline = remote.run(task, "baseline", "baseline")
    alternative = remote.run(task, "probe", "alternative").model_copy(
        update={"probe": probes()[1], "reward": 0.0}
    )
    for item in (baseline, alternative):
        (Path(item.result).parent / "verifier").mkdir()
    (Path(baseline.result).parent / "verifier/result.json").write_text("x" * 100000)
    (Path(baseline.result).parent / "verifier/stdout.txt").write_text("y" * 100000)
    (Path(alternative.result).parent / "verifier/stdout.txt").write_text(
        "FAILED test_root_atomicity: actual != expected"
    )
    context = EvidenceContext(task, [baseline, alternative], limit=16000)
    assert "FAILED test_root_atomicity" in context.documents["evidence/1-probe/verifier/stdout.txt"]
    script_path = "evidence/1-probe/probe-script.sh"
    context.read_more([ReadRequest(path=script_path, query=None, start_line=1, end_line=2)])
    assert context.documents[script_path + ":L1-L2"] == probes()[1].script


def test_immutable_oracle_cannot_be_changed_by_component_repair(task, tmp_path):
    class OracleEditingModel(Model):
        def ask(self, schema, model, system, user, key):
            if schema is Repair:
                return Repair(
                    explanation="Attempt to alter the oracle",
                    addressed_issues=["Weak check"],
                    edits=[
                        Edit(
                            path="solution/solve.sh",
                            old="printf '4'",
                            new="printf '5'",
                            executable=True,
                        )
                    ],
                )
            return super().ask(schema, model, system, user, key)

    before = task_identity(task)
    loop = QualityLoop(
        LoopOptions(repair=True),
        tmp_path / "quality",
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="20"),
        model_client=OracleEditingModel(),
        trial_runner=Trials(tmp_path / "evidence"),
        protected_paths=("solution", "environment/source"),
    )
    result = loop.run(task)
    assert result.status == "needs_evidence"
    assert "immutable source or oracle" in result.reasons[0]
    assert task_identity(task) == before
    assert not (tmp_path / "quality/revisions/r1").exists()


def test_selected_unittest_methods_are_in_initial_review_context(task):
    path = task / "tests/source/tests/test_large.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "class InterleaveEvenlyTests:\n    def test_no_iterables(self):\n        assert result == []\n"
    )
    (task / "tests/contract.json").write_text(
        json.dumps(
            {"expected_passes": ["tests.test_large.InterleaveEvenlyTests::test_no_iterables"]}
        )
    )
    context = EvidenceContext(task, [], limit=100000)
    assert "test_no_iterables" in context.documents["tests/source/tests/test_large.py"]


def test_real_build_failure_text_is_available_without_guessing(task, tmp_path):
    path = tmp_path / "evidence/result.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "exception_info": {
                    "exception_message": "ConfigError: Description file README.rst does not exist"
                }
            }
        )
    )
    trial = TrialRecord(
        role="oracle",
        bundle_hash=task_identity(task),
        result=str(path),
        result_sha256=digest(path),
        agent="oracle",
        model=None,
        reward=None,
        exception_type="RuntimeError",
        binding="receipt",
    )
    context = EvidenceContext(task, [trial], limit=100000)
    assert (
        "Description file README.rst does not exist"
        in context.documents["evidence/0-oracle/result.json"]
    )


def test_selected_class_wins_over_generic_method_names_and_source_copies(task, tmp_path):
    private = task / "tests/source/tests/test_large.py"
    private.parent.mkdir(parents=True)
    private.write_text(
        "\n".join(
            f"class Unrelated{i}:\n    def test_empty(self):\n        assert unrelated == []\n"
            for i in range(250)
        )
        + "\nclass ValueChainTests:\n    def test_empty(self):\n        assert selected_regression == []\n"
    )
    unrelated = task / "tests/source/library/more.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("def more():\n    pass\n" * 200)
    (task / "tests/contract.json").write_text(
        json.dumps(
            {
                "expected_passes": ["tests.test_large.ValueChainTests::test_empty"],
                "test_paths": ["tests/test_large.py::ValueChainTests"],
            }
        )
    )
    refresh_identity(task)
    evidence = Trials(tmp_path / "results").run(task, "baseline", "baseline")
    context = EvidenceContext(task, [evidence], limit=100000)
    assert "selected_regression" in context.documents["tests/source/tests/test_large.py"]
    assert "tests/source/library/more.py" not in context.documents


def test_review_reads_missing_evidence_before_demanding_probe_scripts(task, tmp_path):
    class Reader(Model):
        def ask(self, schema, model, system, user, key):
            payload = json.loads(user)
            self.calls.append(payload)
            if len(self.calls) == 1:
                return review().model_copy(
                    update={
                        "read_requests": [
                            ReadRequest(
                                path="tests/test.sh", query="weak check", start_line=1, end_line=1
                            )
                        ]
                    }
                )
            assert "tests/test.sh:search=weak check" in payload["documents"]
            return review(propose=True)

    model = Reader()
    loop = make_loop(tmp_path, model=model, max_read_rounds=1)
    result, _ = loop._review(task, [], "read", probe_limit=2)
    assert not result.read_requests
    assert len(model.calls) == 2


def test_review_reserves_a_slot_for_valid_alternative(task, tmp_path):
    class WrongOnly(Model):
        def ask(self, schema, model, system, user, key):
            payload = json.loads(user)
            self.calls.append(payload)
            assert payload["required_probe_kinds"] == ["valid_alternative", "wrong_solution"]
            value = review(propose=True)
            if len(self.calls) == 1:
                value.probes = [probes()[0], probes()[0].model_copy(update={"name": "other-wrong"})]
            else:
                assert "missing control kinds" in payload["protocol_feedback"][-1]
            return value

    model = WrongOnly()
    value, _ = make_loop(tmp_path, model=model)._review(task, [], "kinds", probe_limit=2)
    assert {p.kind for p in value.probes} == {"wrong_solution", "valid_alternative"}
    assert len(model.calls) == 2


def test_blocking_task_defect_can_be_repaired_before_authoring_probes(task, tmp_path):
    class Defect(Model):
        def ask(self, *args):
            return review(broken=True, propose=False)

    value, _ = make_loop(tmp_path, model=Defect())._review(task, [], "defect", probe_limit=2)
    assert value.issues and not value.probes


def test_review_inventory_preserves_readable_paths_without_duplicate_hash_tokens(task):
    context = EvidenceContext(task, [], limit=100000)
    for index in range(600):
        key = f"evidence/3-probe/artifacts/workspace/large_package/module_{index:04}.py"
        context.inventory.append({"path": key, "bytes": 200, "sha256": "a" * 64})
        context.omitted.append(key + ": context budget")
    payload = json.loads(context.payload())
    assert len(payload["inventory"]) == len(context.inventory)
    assert payload["inventory"][-1]["path"] == key
    assert payload["budget_omissions"]["count"] >= 600
    assert all("sha256" not in item for item in payload["inventory"])
    assert all("sha256" in item for item in context.inventory)


def test_original_task_intent_and_protected_paths_reach_reviewer(task, tmp_path):
    class IntentModel(Model):
        def ask(self, schema, model, system, user, key):
            payload = json.loads(user)
            assert payload["protected_paths"] == ["solution"]
            context = json.loads(payload["documents"]["evidence/task-context.json"])
            assert context["kind"] == "merged_pr"
            assert context["source_diff"] == "fixed original change"
            return review()

    loop = QualityLoop(
        LoopOptions(),
        tmp_path / "review",
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="5"),
        protected_paths=("solution",),
        task_context={"kind": "merged_pr", "source_diff": "fixed original change"},
        model_client=IntentModel(),
    )
    loop._review(task, [], "intent", probe_limit=0)
