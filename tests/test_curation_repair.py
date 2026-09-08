from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from repo2rlenv.curation import campaign
from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.models import (
    CRITERIA,
    CampaignConfig,
    Contract,
    Review,
    TrialEvidence,
    review_scores,
)
from repo2rlenv.curation.protocol import DraftLimitExceeded, DraftTracker, MechanicalTracker
from repo2rlenv.curation.repair import BoundRepairFile, RepairError, SeedRepair, prepare_seed_repair


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def bound(path):
    return BoundRepairFile(path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return bound(path)


@pytest.fixture
def family(tmp_path):
    parent = tmp_path / "parent"
    task = parent / "revision-0/task"
    source = {
        "id": "example-project-1",
        "url": "https://github.com/example/project/pull/1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    config = CampaignConfig(
        submission_policy="conversion",
        acceptance_policy="validity",
        require_verification_plan=True,
        specification_review=True,
        verifier_review=True,
        max_candidate_drafts=6,
        max_mechanical_submissions=6,
        max_revisions=4,
    )
    contract = {
        "title": "Observe independently defined behavior",
        "rationale": "Meaningful behavior",
        "source_paths": ["src/pkg"],
        "min_tests": 3,
        "requirements": [
            {"id": f"r{i}", "behavior": f"Required observable result {i}", "tests": [f"test_r{i}"]}
            for i in range(2)
        ],
        "mutations": [
            {"name": f"wrong{i}", "rationale": "Break a required result", "script": "false"}
            for i in range(2)
        ],
        "equivalents": [
            {"name": "other", "rationale": "Preserve public results", "script": "true"}
        ],
    }
    write(task / "contract.json", contract)
    (task / "instruction.md").write_text("Parent task with a documented coverage gap.")
    digest = digest_task(task)
    design = {
        "source_digest": hashlib.sha256(canonical(source)).hexdigest(),
        "design": {
            "task_request": "Implement the complete public behavior using independent observable expectations.",
            "verification_plan": {
                "offline_dependencies": "Pinned local Python and NumPy; no runtime network access.",
                "artifact_boundary": "Copy the complete src/pkg tree into the isolated verifier.",
                "behaviors": [
                    {
                        "requirement": f"r{i}",
                        "expected_result": "The independently known result must match for this public condition.",
                        "tests": [f"test_r{i}"],
                        "mutations": [f"wrong{i}"],
                        "equivalents": ["other"],
                    }
                    for i in range(2)
                ],
            },
        },
    }
    result = Review(
        criteria={
            name: {
                "score": 3,
                "outcome": "pass",
                "explanation": "The retained evidence supports this observation.",
                "evidence": ["task/instruction.md"],
            }
            for name in CRITERIA
        },
        blockers=[],
        reward_hacks=[],
        suggested_repairs=[],
        failure_attribution={},
        adversary_assessment="attempted_hack",
    )
    ledger = tmp_path / "budget.json"
    prior = Budget(
        ledger, 380, scope="parent-scope", scope_limit=8, group="parent-group", group_limit=8
    )
    key = prior.reserve(4, "prior model")
    prior.settle(key, 2.75)
    prior.reserve(0.5, "interrupted parent request")
    entries = json.loads(ledger.read_text())["entries"]
    receipt = {
        "ledger_path": str(ledger),
        "global_limit": 380,
        "parent": {
            "scope": "parent-scope",
            "scope_limit": 8,
            "group": "parent-group",
            "group_limit": 8,
        },
        "child": {
            "scope": "repair-scope",
            "scope_limit": 8,
            "group": "repair-group",
            "group_limit": 8,
        },
        "parent_entries_sha256": hashlib.sha256(canonical(entries)).hexdigest(),
        "lineage_scopes": ["parent-scope", "repair-scope"],
        "lineage_limit": 16,
        "phase_groups": ["parent-group", "repair-group"],
        "phase_limit": 40,
    }
    trace = parent / "author-0.jsonl"
    write(trace, {"kind": "model", "turn": 0})
    context = SeedRepair(
        evidence_root=tmp_path,
        parent_root=parent,
        parent_task_digest=digest,
        source=write(parent / "source.json", source),
        config=write(tmp_path / "config.json", config.model_dump()),
        design=write(parent / "design.json", design),
        semantic_history=write(
            parent / "submitted-drafts.json",
            [
                {"digest": "a" * 64, "task": str(parent / "drafts/1/task")},
                {"digest": digest, "task": str(task)},
            ],
        ),
        mechanical_history=write(
            parent / "mechanical-submissions.json",
            [{"task": str(parent / "drafts/0/task"), "reason": "Missing plan mapping"}],
        ),
        author_traces=[bound(trace)],
        review=write(tmp_path / "judge/review.json", result.model_dump()),
        review_result=write(
            tmp_path / "judge/result.json",
            {
                "task_digest": digest,
                "review": result.model_dump(),
                **review_scores(result, config),
                "automatic_gate_reasons": ["Validity score below threshold"],
            },
        ),
        audit=write(
            tmp_path / "audit.json",
            {
                "task_digest": digest,
                "findings": [
                    "An independent audit suggests checking a missing boundary; this is not an executed exploit."
                ],
            },
        ),
        budget_receipt=write(tmp_path / "repair-budget.json", receipt),
    )
    budget = Budget(
        ledger, 380, scope="repair-scope", scope_limit=8, group="repair-group", group_limit=8
    )
    return SimpleNamespace(
        parent=parent,
        task=task,
        root=tmp_path / "child",
        context=context,
        source=source,
        config=config,
        budget=budget,
        receipt=receipt,
        review=result,
        contract=contract,
    )


def prepare(f):
    return prepare_seed_repair(f.context, f.task, f.root, f.source, f.config, f.budget)


def update_input(f, field, value):
    f.context = f.context.model_copy(update={field: write(getattr(f.context, field).path, value)})


def test_claim_inherits_histories_and_exact_labelled_feedback_without_spend(family):
    f = family
    old_ledger = f.budget.path.read_bytes()
    old_task = digest_task(f.task)
    with prepare(f) as repair:
        assert repair.used == 1
        assert len(json.loads((f.root / "submitted-drafts.json").read_text())) == 2
        assert len(json.loads((f.root / "mechanical-submissions.json").read_text())) == 1
        assert repair.restore_task(f.task) == f.task
        assert repair.design.model_dump() == json.loads(f.context.design.path.read_text())["design"]
        assert "Historical judge review" in repair.feedback
        assert "Validity score below threshold" in repair.feedback
        assert "Independent audit suggestions" in repair.feedback
        assert "not an executed exploit" in repair.feedback
        assert not (f.root / "revision-0").exists()
    state = json.loads(f.budget.path.read_text())
    assert state["entries"] == json.loads(old_ledger)["entries"]
    assert "repair-scope" in state["scope_constraints"]
    assert digest_task(f.task) == old_task
    assert json.loads((f.parent / "repair-child.json").read_text())["root"] == str(f.root)


@pytest.mark.parametrize(
    "damage",
    [
        "task",
        "source",
        "config",
        "design",
        "audit",
        "review",
        "score",
        "budget",
        "parent_entries",
        "traces",
        "symlink",
        "outside",
        "oversized",
    ],
)
def test_invalid_repair_inputs_fail_before_claim_or_spend(family, damage):
    f = family
    ledger = f.budget.path.read_bytes()
    if damage == "task":
        (f.task / "instruction.md").write_text("changed")
    elif damage == "source":
        f.source = {**f.source, "head_sha": "c" * 40}
    elif damage == "config":
        f.config = f.config.model_copy(update={"max_revisions": 5})
    elif damage == "design":
        value = json.loads(f.context.design.path.read_text())
        value["source_digest"] = "f" * 64
        update_input(f, "design", value)
    elif damage == "audit":
        update_input(f, "audit", {"task_digest": "f" * 64})
    elif damage == "review":
        f.context.review.path.write_text("{}")
    elif damage == "score":
        value = json.loads(f.context.review_result.path.read_text())
        value["score"] = 99
        update_input(f, "review_result", value)
    elif damage == "budget":
        f.budget.scope_limit = 9
    elif damage == "parent_entries":
        f.receipt["parent_entries_sha256"] = "f" * 64
        update_input(f, "budget_receipt", f.receipt)
    elif damage == "traces":
        write(f.parent / "author-1.jsonl", {"kind": "model"})
    elif damage == "symlink":
        path = f.context.audit.path
        target = path.with_name("real-audit.json")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "outside":
        f.context = f.context.model_copy(update={"evidence_root": f.parent})
    else:
        update_input(
            f, "audit", {"task_digest": f.context.parent_task_digest, "finding": "x" * 65_000}
        )
    with pytest.raises((RepairError, ValueError)):
        with prepare(f):
            pytest.fail("Must reject before authoring")
    assert not (f.parent / "repair-child.json").exists()
    assert f.budget.path.read_bytes() == ledger


@pytest.mark.parametrize("field", ["semantic_history", "mechanical_history"])
def test_exhausted_inherited_submissions_stop_before_claim(family, field):
    f = family
    rows = json.loads(getattr(f.context, field).path.read_text())
    while len(rows) < 6:
        rows.append(
            {
                "task": str(f.parent / f"drafts/{len(rows)}/task"),
                **(
                    {"digest": f"{len(rows):064x}"}
                    if field == "semantic_history"
                    else {"reason": "Invalid input"}
                ),
            }
        )
    update_input(f, field, rows)
    with pytest.raises(RepairError, match="allowance exhausted"):
        with prepare(f):
            pass
    assert not (f.parent / "repair-child.json").exists()


def test_repair_counters_do_not_reset_on_restart_or_new_child(family):
    f = family
    with prepare(f) as repair:
        repair.start_revision(1)
        drafts = DraftTracker(f.root / "submitted-drafts.json", 6)
        for i in range(3, 7):
            drafts.observe(f"{i:064x}", f.root / f"drafts/{i}/task")
        with pytest.raises(DraftLimitExceeded):
            drafts.observe("f" * 64, f.root / "extra")
        mechanics = MechanicalTracker(f.root / "mechanical-submissions.json", 6)
        mechanics.fail(f.root / "bad", "Missing tests")
        with pytest.raises(RepairError, match="already active"):
            with prepare(f):
                pass
    with pytest.raises(RepairError, match="submission allowance exhausted"):
        with prepare(f):
            pass
    f.root = f.root.with_name("another-child")
    with pytest.raises(RepairError, match="different repair child"):
        with prepare(f):
            pass


def test_restart_keeps_started_revision_and_restores_last_submitted_digest(family):
    f = family
    with prepare(f) as repair:
        repair.start_revision(1)
        checkpoint = f.root / "drafts/1/task"
        shutil.copytree(f.task, checkpoint)
        (checkpoint / "instruction.md").write_text("A repaired public boundary")
        DraftTracker(f.root / "submitted-drafts.json", 6).observe(
            digest_task(checkpoint), checkpoint
        )
    with prepare(f) as repair:
        assert repair.used == 2
        assert repair.restore_task(f.task) == checkpoint
    (f.root / "repair-progress.json").unlink()
    with pytest.raises((RepairError, FileNotFoundError)):
        with prepare(f):
            pass


@pytest.mark.parametrize("gate", ["lineage", "phase"])
def test_new_reservations_enforce_cumulative_caps_but_settlement_is_retained(family, gate):
    f = family
    f.receipt[gate + "_limit"] = 11.25  # Parent3.25 plus the complete child8 fits.
    update_input(f, "budget_receipt", f.receipt)
    with prepare(f) as repair:
        key = repair.budget.reserve(1, "repair model")
        # Simulate another already-paid entry in the same lineage/phase.
        prior = Budget(f.budget.path, 380, scope="parent-scope", group="parent-group")
        parent_charge = prior.reserve(8, "concurrent prior reservation")
        before = json.loads(f.budget.path.read_text())
        plain = Budget(
            f.budget.path,
            380,
            scope="repair-scope",
            scope_limit=8,
            group="repair-group",
            group_limit=8,
        )
        with pytest.raises(BudgetExceeded, match=gate):
            plain.reserve(0.25, "must be rejected atomically even after plain reconstruction")
        assert json.loads(f.budget.path.read_text()) == before
        repair.budget.settle(key, 1.5)
        after = json.loads(f.budget.path.read_text())["entries"]
        assert after[key]["charged_usd"] == 1.5 and after[key]["status"] == "metered"
        assert parent_charge in after


def test_insufficient_lineage_allocation_fails_before_claim(family):
    f = family
    f.receipt["lineage_limit"] = 11
    update_input(f, "budget_receipt", f.receipt)
    with pytest.raises(BudgetExceeded, match="lineage allowance"):
        with prepare(f):
            pass
    assert not (f.parent / "repair-child.json").exists()


@pytest.mark.asyncio
async def test_campaign_repairs_directly_with_remaining_revisions_and_full_new_trials(
    family, monkeypatch
):
    f = family
    sandbox_task = f.root.parent / "mock-sandbox-task"
    sandbox_task.mkdir()
    prompts, observed_trials, reviewed_digests = [], [], []

    class Sandbox:
        def __init__(self, timeout):
            async def copy(src, destination):
                target = sandbox_task / destination.removeprefix("/output/task/")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)

            self.sandbox = SimpleNamespace(
                object_id="fake",
                filesystem=SimpleNamespace(copy_from_local=SimpleNamespace(aio=copy)),
            )

        async def start(self):
            pass

        async def stop(self):
            pass

        async def prepare(self, source):
            assert source == f.source

        async def shell(self, **kwargs):
            return ""

        async def export(self, task):
            shutil.copytree(sandbox_task, task)

    async def author(**kwargs):
        prompts.append(kwargs["prompt"])
        (sandbox_task / "instruction.md").write_text(
            f"Repaired public condition, revision {len(prompts)}"
        )

    async def preflight(task, output, **kwargs):
        return [await trial(task, output, label) for label in ["baseline", "oracle-0"]]

    async def trial(task, output, label, **kwargs):
        observed_trials.append((digest_task(task), label))
        path = output / label
        path.mkdir(parents=True, exist_ok=True)
        return TrialEvidence(
            label=label,
            reward=int(label.startswith(("oracle", "equivalent", "solver"))),
            task_digest=digest_task(task),
            path=str(path),
        )

    async def judge(source, folder, config, budget, contract, digest, trials):
        reviewed_digests.append(digest)
        return {
            "status": "rejected",
            "execution_errors": [],
            "reasons": ["Supported remaining coverage gap"],
        }, f.review

    never = Mock(side_effect=AssertionError("No parent review/planning reroll"))
    monkeypatch.setattr(campaign, "AuthorSandbox", Sandbox)
    monkeypatch.setattr(campaign, "run_agent", author)
    monkeypatch.setattr(campaign, "plan_candidate_design", never)
    monkeypatch.setattr(campaign, "_prepare_pending_review", never)
    monkeypatch.setattr(campaign, "_resume_validation", never)
    monkeypatch.setattr(campaign, "finalize", lambda *args: Contract.model_validate(f.contract))
    monkeypatch.setattr(campaign, "check_verification_plan", lambda *args: None)
    monkeypatch.setattr(campaign, "specification_snapshot", lambda *args: None)
    monkeypatch.setattr(campaign, "verifier_snapshot", lambda *args: None)
    monkeypatch.setattr(
        campaign, "review_specification", AsyncMock(return_value=SimpleNamespace(passed=True))
    )
    monkeypatch.setattr(
        campaign, "review_verifier", AsyncMock(return_value=SimpleNamespace(passed=True))
    )
    monkeypatch.setattr(campaign, "preflight", preflight)
    monkeypatch.setattr(campaign, "trial", trial)
    monkeypatch.setattr(campaign, "_review_revision", judge)
    result = await campaign.curate_one(
        f.source, f.root, f.config, f.budget, f.task, seed_repair=f.context
    )
    assert result["kind"] == "assisted_autonomous_repair"
    assert result["parent_task_digest"] == f.context.parent_task_digest
    assert len(prompts) == 3
    assert (
        "Remaining allowances: 4 semantic submissions, 5 mechanical failures, 3 author revisions"
        in prompts[0]
    )
    assert "Historical judge review" in prompts[0] and "Independent audit suggestions" in prompts[0]
    assert "infrastructure issues have been fixed" not in prompts[0]
    assert len(reviewed_digests) == 3 and f.context.parent_task_digest not in reviewed_digests
    expected = {
        "baseline",
        "oracle-0",
        "oracle-1",
        "oracle-2",
        "tamper",
        "pytest-tamper",
        "mutation-wrong0",
        "mutation-wrong1",
        "equivalent-other",
        "solver-0-0",
        "solver-1-0",
        "adversary",
    }
    for digest in reviewed_digests:
        assert {label for actual, label in observed_trials if actual == digest} == expected
    assert json.loads((f.root / "repair-progress.json").read_text())["used_author_revisions"] == 4
    assert len(json.loads((f.root / "submitted-drafts.json").read_text())) == 5
    assert digest_task(f.task) == f.context.parent_task_digest
    never.assert_not_called()


@pytest.mark.asyncio
async def test_conversion_seed_without_context_remains_rejected(family, monkeypatch):
    f = family
    sandbox = Mock(side_effect=AssertionError("No sandbox before provenance"))
    monkeypatch.setattr(campaign, "AuthorSandbox", sandbox)
    with pytest.raises(campaign.RecoveryError, match="fresh candidate"):
        await campaign.curate_one(f.source, f.root, f.config, f.budget, f.task)
    sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_interrupted_author_consumes_revision_before_retry(family, monkeypatch):
    f = family

    async def interrupted(source, root, config, budget, seed_task, *, repair):
        repair.start_revision(repair.used)
        raise asyncio.CancelledError

    monkeypatch.setattr(campaign, "_curate_one", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await campaign.curate_one(
            f.source, f.root, f.config, f.budget, f.task, seed_repair=f.context
        )
    with prepare(f) as repair:
        assert repair.used == 2


def test_unchanged_parent_and_already_judged_child_cannot_reroll(family):
    f = family
    with prepare(f) as repair:
        with pytest.raises(ValueError, match="unchanged parent"):
            repair.require_unreviewed_change(f.context.parent_task_digest)
        write(f.root / "revision-1/review.json", f.review.model_dump())
        write(f.root / "revision-1/evidence.json", {"task_digest": "c" * 64})
        with pytest.raises(ValueError, match="already has a final review"):
            repair.require_unreviewed_change("c" * 64)
        repair.require_unreviewed_change("d" * 64)
        assert len(json.loads((f.root / "submitted-drafts.json").read_text())) == 2


@pytest.mark.parametrize("filename", ["verdict.json", "repair-result.json"])
def test_terminal_child_requires_recovery_instead_of_another_author(family, filename):
    f = family
    with prepare(f):
        write(f.root / filename, {"status": "accepted"})
    with pytest.raises(RepairError, match=r"recovery|terminal result"):
        with prepare(f):
            pass


def test_repair_accounting_counts_parent_entries_and_child_delta_once(family):
    f = family
    with prepare(f) as repair:
        key = repair.budget.reserve(1, "new model")
        repair.budget.settle(key, 0.75)
        result = repair.accounting()
        assert result["lineage_charged_or_reserved_usd"] == 4
        assert result["repair_charged_or_reserved_usd"] == 0.75
        assert len(result["lineage_entry_ids"]) == 3
        assert result["repair_scope"] == "repair-scope"


def test_copied_parent_cannot_claim_another_child_with_same_allowance(family):
    f = family
    copied = f.parent.with_name("copied-parent")
    shutil.copytree(f.parent, copied)
    with prepare(f):
        pass
    replacements = {"parent_root": copied}
    for name in ("source", "design", "semantic_history", "mechanical_history"):
        original = getattr(f.context, name)
        replacements[name] = bound(copied / original.path.relative_to(f.parent))
    replacements["author_traces"] = [
        bound(copied / item.path.name) for item in f.context.author_traces
    ]
    f.context = f.context.model_copy(update=replacements)
    f.task = copied / f.task.relative_to(f.parent)
    f.root = f.root.with_name("second-child")
    with pytest.raises(RepairError, match=r"shared ledger.*different repair child"):
        with prepare(f):
            pass
    assert not (copied / "repair-child.json").exists()
