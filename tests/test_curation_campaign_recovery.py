from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from repo2rlenv.curation import campaign
from repo2rlenv.curation.artifacts import digest_task, release_task
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.campaign import ADMISSION_VERSION
from repo2rlenv.curation.inference import inference_digest
from repo2rlenv.curation.models import CRITERIA, CampaignConfig, Review, TrialEvidence

URL = "https://github.com/example/project/pull/1"
IDENTITY = "example-project-1"


@pytest.fixture(autouse=True)
def recorded_package_versions(monkeypatch):
    # Recovery uses mocked execution; optional Harbor installation is unrelated
    # to the persistence and budget behavior exercised by these tests.
    monkeypatch.setattr(campaign, "version", lambda name: "test-version")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def manifest():
    return {"target": 30, "accepted": [], "rejected": [], "seeds": [URL]}


@pytest.fixture
def accepted(tmp_path):
    root = tmp_path.resolve() / "stage2"
    config = CampaignConfig(specification_review=True, verifier_review=True, concurrency=4)
    write(root / "config.json", config.model_dump())
    task = root / "candidates" / IDENTITY / "123" / "revision-0" / "task"
    write(
        task / "contract.json",
        {
            "title": "A substantive behavior change",
            "rationale": "Observe independent results",
            "source_paths": ["src/project"],
            "min_tests": 3,
            "requirements": [
                {"id": "r1", "behavior": "Returns exact values", "tests": ["a", "b"]},
                {"id": "r2", "behavior": "Handles alternatives", "tests": ["c"]},
            ],
            "mutations": [
                {"name": name, "rationale": "Break observable behavior", "script": "false"}
                for name in ["zero", "wrong"]
            ],
            "equivalents": [
                {"name": "other", "rationale": "Preserve the behavior", "script": "true"}
            ],
        },
    )
    digest = digest_task(task)
    trials = [
        TrialEvidence(label=label, task_digest=digest, reward=reward, path="retained")
        for label, reward in [
            ("baseline", 0),
            ("tamper", 0),
            ("pytest-tamper", 0),
            ("mutation-zero", 0),
            ("mutation-wrong", 0),
            ("equivalent-other", 1),
            *[(f"oracle-{i}", 1) for i in range(config.oracle_repeats)],
        ]
    ]
    for i, model in enumerate(config.solver_models):
        trials.append(
            TrialEvidence(
                label=f"solver-{i}-0",
                task_digest=digest,
                reward=1,
                path="retained",
                model=model,
                inference_digest=inference_digest(model),
            )
        )
    trials.append(
        TrialEvidence(
            label="adversary",
            task_digest=digest,
            reward=0,
            path="retained",
            model=config.author_model,
            inference_digest=inference_digest(config.author_model, adversary=True),
        )
    )
    review = Review(
        criteria={
            key: {
                "score": 4,
                "outcome": "pass",
                "explanation": "The observed evidence satisfies the criterion.",
                "evidence": ["retained trace"],
            }
            for key in CRITERIA
        },
        blockers=[],
        failure_attribution={t.label: "solved" for t in trials if t.label.startswith("solver-")},
        reward_hacks=[],
        suggested_repairs=[],
        adversary_assessment="attempted_hack",
    )
    write(task.parent / "review.json", review.model_dump())
    write(
        task.parent / "evidence.json",
        {
            "admission_version": ADMISSION_VERSION,
            "task_digest": digest,
            "trials": [t.model_dump() for t in trials],
        },
    )
    write(task.parent.parent / "source.json", {"id": IDENTITY, "url": URL})
    row = {
        "id": IDENTITY,
        "source": URL,
        "task_digest": digest,
        "status": "accepted",
        "execution_errors": [],
        "score": review.score,
        "reasons": [],
        "task_path": str(task),
        "human_review": "pending",
        "admission_version": ADMISSION_VERSION,
        "review_path": str(task.parent / "review.json"),
    }
    write(task.parent.parent / "verdict.json", row)
    budget = Budget(tmp_path / "shared-budget.json", 380)
    state = manifest()
    scoped = campaign._candidate_budget(root, state, URL, budget, config)
    key = scoped.reserve(4, "prior model attempt")
    scoped.settle(key, 2.75)
    scoped.reserve(1, "interrupted request")
    return root, config, state, budget, task, row


def test_interrupted_scope_preserves_settled_and_reserved_spend(tmp_path):
    root = tmp_path.resolve()
    state, config = manifest(), CampaignConfig(max_candidate_usd=5)
    budget = Budget(root / "budget.json", 380)
    scoped = campaign._candidate_budget(root, state, URL, budget, config)
    persisted = json.loads((root / "manifest.json").read_text())
    assert persisted["budget_scopes"][URL] == scoped.scope
    key = scoped.reserve(3, "completed")
    scoped.settle(key, 2)
    scoped.reserve(2, "interrupted")
    recovered = campaign._candidate_budget(root, persisted, URL, budget, config)
    assert recovered.scope == scoped.scope and recovered.spent == 4
    with pytest.raises(BudgetExceeded, match="Candidate budget"):
        recovered.reserve(2, "must not reset limit")


def test_legacy_unrecorded_scope_recovered_from_shared_ledger(tmp_path):
    root = tmp_path.resolve()
    state, config = manifest(), CampaignConfig()
    old = Budget(root / "budget.json", 380, scope=f"{URL}:123")
    old.reserve(3, "interrupted")
    recovered = campaign._candidate_budget(root, state, URL, Budget(old.path, 380), config)
    assert recovered.scope == old.scope and recovered.spent == 3
    Budget(old.path, 380, scope=f"{URL}:456").reserve(1, "second old attempt")
    with pytest.raises(campaign.RecoveryError, match="Multiple candidate budget scopes"):
        campaign._candidate_budget(root, state, URL, Budget(old.path, 380), config)


@pytest.mark.parametrize("already_released", [False, True])
def test_accepted_recovery_is_idempotent_and_retains_cumulative_cost(accepted, already_released):
    root, config, state, budget, task, row = accepted
    if already_released:
        release_task(task, root / "tasks" / IDENTITY)
    campaign._reconcile_accepted(root, state, config, budget)
    assert len(state["accepted"]) == 1
    assert state["accepted"][0]["score"] == row["score"]
    assert state["accepted"][0]["charged_or_reserved_usd"] == 3.75
    assert digest_task(root / "tasks" / IDENTITY) == row["task_digest"]
    campaign._reconcile_accepted(root, state, config, budget)
    assert len(state["accepted"]) == 1 and budget.spent == 3.75


def test_interruption_after_release_before_manifest_write_recovers(accepted, monkeypatch):
    root, config, state, budget, _task, row = accepted
    original = campaign.save

    def interrupted(path, data):
        if path.name == "manifest.json" and data.get("accepted"):
            raise KeyboardInterrupt
        original(path, data)

    monkeypatch.setattr(campaign, "save", interrupted)
    with pytest.raises(KeyboardInterrupt):
        campaign._reconcile_accepted(root, state, config, budget)
    assert (root / "tasks" / IDENTITY).is_dir()
    persisted = json.loads((root / "manifest.json").read_text())
    assert not persisted["accepted"]
    monkeypatch.setattr(campaign, "save", original)
    campaign._reconcile_accepted(root, persisted, config, budget)
    assert len(persisted["accepted"]) == 1
    assert persisted["accepted"][0]["task_digest"] == row["task_digest"]


@pytest.mark.parametrize(
    "damage",
    [
        "task",
        "score",
        "version",
        "evidence_digest",
        "missing_trial",
        "solver_policy",
        "review_blocker",
        "audit",
        "config",
        "missing_review",
        "manifest_score",
        "released",
        "orphan",
    ],
)
def test_recovery_rejects_mismatched_or_incomplete_evidence(accepted, damage):
    root, config, state, budget, task, row = accepted
    if damage == "task":
        write(task / "extra.json", {"changed": True})
    elif damage in {"score", "version"}:
        row["score" if damage == "score" else "admission_version"] -= 1
        write(task.parent.parent / "verdict.json", row)
    elif damage in {"evidence_digest", "missing_trial", "solver_policy"}:
        path = task.parent / "evidence.json"
        evidence = json.loads(path.read_text())
        if damage == "evidence_digest":
            evidence["task_digest"] = "wrong"
        elif damage == "missing_trial":
            evidence["trials"].pop(0)
        else:
            next(t for t in evidence["trials"] if t["label"].startswith("solver-"))[
                "inference_digest"
            ] = "old"
        write(path, evidence)
    elif damage in {"review_blocker", "audit"}:
        path = task.parent / "review.json"
        review = json.loads(path.read_text())
        review["blockers" if damage == "review_blocker" else "adversary_assessment"] = (
            ["A confirmed verifier defect"] if damage == "review_blocker" else "solved_task"
        )
        write(path, review)
    elif damage == "config":
        changed = config.model_dump()
        changed["acceptance_score"] = 90
        write(root / "config.json", changed)
    elif damage == "missing_review":
        (task.parent / "review.json").unlink()
    elif damage == "manifest_score":
        state["accepted"].append({**row, "score": 0})
    elif damage == "released":
        release_task(task, root / "tasks" / IDENTITY)
        write(root / "tasks" / IDENTITY / "extra.json", {})
    else:
        write(root / "tasks" / "orphan" / "contract.json", {})
    with pytest.raises(campaign.RecoveryError):
        campaign._reconcile_accepted(root, state, config, budget)
    assert budget.spent == 3.75
    if damage not in {"released", "orphan"}:
        assert not (root / "tasks").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_released", [False, True])
async def test_public_campaign_recovers_accepted_without_model_or_source_calls(
    accepted, monkeypatch, already_released
):
    root, config, _state, budget, task, row = accepted
    if already_released:
        release_task(task, root / "tasks" / IDENTITY)
    # Public campaigns own their output's ledger; source keys and reservations persist.
    budget.path.rename(root / "budget.json")
    curate = AsyncMock(side_effect=AssertionError("No author or judge reroll"))
    resolve = Mock(side_effect=AssertionError("No API call"))
    monkeypatch.setattr(campaign, "curate_one", curate)
    monkeypatch.setattr(campaign, "resolve_pr", resolve)
    result = await campaign.campaign([URL], root, config)
    assert len(result["accepted"]) == 1
    assert result["accepted"][0]["charged_or_reserved_usd"] == 3.75
    assert result["accepted"][0]["score"] == row["score"]
    assert digest_task(root / "tasks" / IDENTITY) == row["task_digest"]
    result = await campaign.campaign([URL], root, config)
    assert len(result["accepted"]) == 1
    curate.assert_not_called()
    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_public_campaign_interrupt_resume_keeps_source_cap(tmp_path, monkeypatch):
    config = CampaignConfig(max_candidate_usd=5, concurrency=1)
    scopes = []
    started = asyncio.Event()

    async def interrupted(source, root, config, budget, seed_task):
        scopes.append(budget.scope)
        key = budget.reserve(3, "completed author request")
        budget.settle(key, 2)
        budget.reserve(2, "interrupted provider request")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(campaign, "resolve_pr", lambda _: {"url": URL, "id": IDENTITY})
    monkeypatch.setattr(campaign, "curate_one", interrupted)
    running = asyncio.create_task(campaign.campaign([URL], tmp_path, config))
    await asyncio.wait_for(started.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    state = json.loads((tmp_path / "manifest.json").read_text())
    assert state["budget_scopes"][URL] == scopes[0]
    assert state["in_progress"] == [URL]

    async def resumed(source, root, config, budget, seed_task):
        scopes.append(budget.scope)
        assert budget.spent == 4
        budget.reserve(2, "cannot reset per-source allowance")
        raise AssertionError("The cap must stop this request")

    monkeypatch.setattr(campaign, "curate_one", resumed)
    result = await campaign.campaign([URL], tmp_path, config)
    assert scopes == [scopes[0], scopes[0]]
    assert result["rejected"][0]["charged_or_reserved_usd"] == 4
    assert "Candidate budget" in result["rejected"][0]["reasons"][0]
    assert Budget(tmp_path / "budget.json", config.budget_usd).spent == 4


@pytest.mark.asyncio
async def test_explicit_retry_preserves_scope_and_previous_attempt_cost(tmp_path, monkeypatch):
    config = CampaignConfig(max_candidate_usd=5, concurrency=1)
    scopes = []

    async def rejected(source, root, config, budget, seed_task):
        scopes.append(budget.scope)
        budget.reserve(2, "failed attempt")
        return {"source": URL, "status": "rejected", "reasons": ["Repair needed"]}

    monkeypatch.setattr(campaign, "resolve_pr", lambda _: {"url": URL, "id": IDENTITY})
    monkeypatch.setattr(campaign, "curate_one", rejected)
    await campaign.campaign([URL], tmp_path, config)
    result = await campaign.campaign([URL], tmp_path, config, retry_rejected=True)
    assert scopes[0] == scopes[1]
    assert result["previous_attempts"][0]["charged_or_reserved_usd"] == 2
    assert result["rejected"][0]["charged_or_reserved_usd"] == 4
    assert Budget(tmp_path / "budget.json", config.budget_usd).spent == 4


@pytest.mark.asyncio
async def test_ambiguous_legacy_campaign_stops_before_work(tmp_path, monkeypatch):
    config = CampaignConfig(concurrency=1)
    write(tmp_path / "config.json", config.model_dump())
    write(tmp_path / "manifest.json", manifest())
    for number in [123, 456]:
        Budget(tmp_path / "budget.json", config.budget_usd, scope=f"{URL}:{number}").reserve(
            1, "old request"
        )
    before = (tmp_path / "budget.json").read_bytes()
    author = AsyncMock(side_effect=AssertionError("No reroll on ambiguous spend"))
    resolver = Mock(side_effect=AssertionError("No source call"))
    monkeypatch.setattr(campaign, "curate_one", author)
    monkeypatch.setattr(campaign, "resolve_pr", resolver)
    with pytest.raises(campaign.RecoveryError, match="Multiple candidate budget scopes"):
        await campaign.campaign([URL], tmp_path, config)
    assert json.loads((tmp_path / "budget.json").read_bytes()) == json.loads(before)
    author.assert_not_called()
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_recorded_old_admission_still_archives_and_revalidates(accepted, monkeypatch):
    root, config, state, budget, task, row = accepted
    row["admission_version"] = ADMISSION_VERSION - 1
    write(task.parent.parent / "verdict.json", row)
    state["accepted"].append(row)
    write(root / "manifest.json", state)
    release_task(task, root / "tasks" / IDENTITY)
    budget.path.rename(root / "budget.json")
    author = AsyncMock(
        return_value={"source": URL, "status": "rejected", "reasons": ["Revalidation needs repair"]}
    )
    monkeypatch.setattr(campaign, "resolve_pr", lambda _: {"id": IDENTITY, "url": URL})
    monkeypatch.setattr(campaign, "curate_one", author)
    result = await campaign.campaign([URL], root, config)
    assert not result["accepted"]
    previous = result["previous_attempts"][0]
    assert previous["status"] == "needs_revalidation"
    assert digest_task(Path(previous["archived_task"])) == row["task_digest"]
    assert not (root / "tasks" / IDENTITY).exists()
    assert author.await_args.args[-1] == task
    assert author.await_args.args[3].spent == 3.75
    # A recorded old orphan remains recognized after that migration.
    result = await campaign.campaign([URL], root, config)
    assert not result["accepted"] and author.await_count == 1


@pytest.mark.parametrize(
    "linked", ["review.json", "evidence.json", "verdict.json", "source.json", "contract.json"]
)
def test_accepted_recovery_refuses_linked_metadata(accepted, linked):
    root, config, state, budget, task, _row = accepted
    folder = (
        task
        if linked == "contract.json"
        else (task.parent.parent if linked in {"verdict.json", "source.json"} else task.parent)
    )
    path = folder / linked
    external = root.parent / linked
    path.rename(external)
    path.symlink_to(external)
    with pytest.raises((campaign.RecoveryError, ValueError), match=r"[Ll]ink|Symlink"):
        campaign._reconcile_accepted(root, state, config, budget)
    assert not (root / "tasks").exists()


@pytest.mark.parametrize(
    "damage", ["outside", "source", "attempt", "revision", "manifest_identity"]
)
def test_accepted_recovery_checks_exact_candidate_provenance(accepted, damage):
    root, config, state, budget, task, row = accepted
    path = task.parent.parent / "verdict.json"
    if damage == "outside":
        row["task_path"] = str(root.parent / "elsewhere" / "task")
    elif damage == "source":
        write(task.parent.parent / "source.json", {"id": IDENTITY, "url": URL + "0"})
    elif damage == "attempt":
        other = root / "candidates" / IDENTITY / "456"
        other.mkdir()
        path.rename(other / "verdict.json")
        path = other / "verdict.json"
    elif damage == "revision":
        row["task_path"] = str(task.parent.with_name("revision-other") / "task")
    else:
        row["admission_version"] -= 1
        state["accepted"].append({**row, "id": "../escape"})
    write(path, row)
    with pytest.raises(campaign.RecoveryError):
        campaign._reconcile_accepted(root, state, config, budget)
    assert not (root / "tasks").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["config.json", "budget.json"])
async def test_public_recovery_requires_original_protocol_and_ledger(
    accepted, monkeypatch, missing
):
    root, config, _state, budget, _task, _row = accepted
    budget.path.rename(root / "budget.json")
    (root / missing).unlink()
    author = AsyncMock(side_effect=AssertionError("No author call"))
    monkeypatch.setattr(campaign, "curate_one", author)
    with pytest.raises(campaign.RecoveryError, match="original configuration and budget ledger"):
        await campaign.campaign([URL], root, config)
    author.assert_not_called()


@pytest.mark.asyncio
async def test_manifest_before_first_request_still_has_resumable_empty_ledger(
    tmp_path, monkeypatch
):
    config = CampaignConfig(concurrency=1)
    original = campaign._candidate_budget

    def interrupt_after_scope(*args):
        original(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(campaign, "_candidate_budget", interrupt_after_scope)
    with pytest.raises(KeyboardInterrupt):
        await campaign.campaign([URL], tmp_path, config)
    assert json.loads((tmp_path / "budget.json").read_text()) == {"entries": {}}
    monkeypatch.setattr(campaign, "_candidate_budget", original)
    monkeypatch.setattr(campaign, "resolve_pr", lambda _: {"id": IDENTITY, "url": URL})
    monkeypatch.setattr(
        campaign,
        "curate_one",
        AsyncMock(return_value={"source": URL, "status": "rejected", "reasons": []}),
    )
    result = await campaign.campaign([URL], tmp_path, config)
    assert len(result["rejected"]) == 1
