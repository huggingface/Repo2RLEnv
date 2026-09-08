from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.tasksmith import finalize as f
from repo2rlenv.tasksmith.authoring import Construction
from repo2rlenv.tasksmith.emit import emit_task, verify_collection
from repo2rlenv.tasksmith.models import (
    QUALITY_DIMENSIONS,
    Deadline,
    LedgerRef,
    OperationKey,
    PRIdentity,
    QualityReport,
)
from repo2rlenv.tasksmith.state import Journal, ReconciliationRequired
from repo2rlenv.tasksmith.worker import save_json
from tests.test_tasksmith_validation import binding_inputs as _binding_inputs


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def retained(tmp_path):
    """Entirely synthetic controller receipts; no artifact or provider code runs."""
    pytest.importorskip("harbor")
    config, source, construction, _ = _binding_inputs.__wrapped__(tmp_path)
    source.update(id="example-12", package_name="example")
    construction["source_receipt"]["source"] = dict(source)
    patch = "--- a/src/example/__init__.py\n+++ b/src/example/__init__.py\n"
    construction["source_receipt"]["patch_digest"] = hashlib.sha256(patch.encode()).hexdigest()
    construction["readiness"] = {"passed": True, "checks": [{"exit_code": 0, "stdout": "ok"}]}
    parent_root = tmp_path / "candidate"
    task = parent_root / "revision-1/task"
    validation = task.parent / "validation-attempt-2"
    value = Construction.model_validate(construction["artifact"])
    construction["emitter"] = emit_task(
        task,
        source,
        "FROM python:3.12-slim@sha256:" + "c" * 64 + "\nRUN python -m pip install pytest==8.4.2\n",
        execution_contract=value.contract,
        instruction=construction["public_instruction"],
        solution_script="#!/bin/sh\ntrue\n",
        protected_tests="from probe import run_probe\n"
        + "\n".join(
            f'def {name}():\n    assert run_probe("print(json.dumps(1))") == 1\n'
            for name in ("test_order", "test_tail", "test_empty")
        ),
        source_origin_probe=value.source_origin_probe,
    )
    construction["task_path"] = str(task)
    revision = digest_task(task)
    deadline = 2000.0
    now = [1000.0]
    journal = Journal(tmp_path / "journal.sqlite", clock=lambda: now[0])
    pr = PRIdentity.from_url(source["url"])
    journal.register_pr(
        pr,
        deadline=Deadline(expires_at=deadline),
        ledger=LedgerRef(path=str(config.ledger_path), scope="fixture:example-12", group="fixture"),
    )
    lease = journal.acquire_lease(pr.key, owner="test", ttl_seconds=900)
    journal.register_revision(
        lease, revision=0, task_digest="a" * 64, parent_digest=None, manifest={"input": source}
    )
    journal.register_revision(
        lease,
        revision=1,
        task_digest=revision,
        parent_digest="a" * 64,
        manifest={"task_digest": revision},
    )
    construct_key = OperationKey(
        pr_id=pr.key,
        revision_digest=revision,
        stage="construction",
        effect="construct",
        input_digest="b" * 64,
        policy_digest=config.digest(),
        attempt=0,
    )
    journal.claim_operation(lease, construct_key)
    journal.commit_artifact(
        lease,
        construct_key.operation_id,
        name="result",
        data=json.dumps(construction, sort_keys=True).encode(),
    )
    journal.finish_operation(lease, construct_key.operation_id, receipt={"done": True})
    key = OperationKey(
        pr_id=pr.key,
        revision_digest=revision,
        stage="validation",
        effect="validation",
        input_digest=f.canonical_digest({"task": revision}),
        policy_digest=config.digest(),
        attempt=2,
    )
    retry_of = None
    for attempt in (0, 1):
        prior = key.model_copy(update={"attempt": attempt})
        journal.claim_operation(lease, prior, retry_of=retry_of)
        journal.finish_operation(
            lease,
            prior.operation_id,
            receipt={"cleanup_confirmed": True},
            failed=True,
            error="Retained failed attempt",
        )
        retry_of = prior.operation_id
    journal.claim_operation(lease, key, retry_of=retry_of)
    save_json(
        parent_root / "identity.json",
        {
            "config_digest": config.digest(),
            "source_digest": f.canonical_digest(source),
            "deadline": deadline,
        },
    )
    write(task.parent / "gold.patch", patch)
    patch_ref = journal.commit_artifact(
        lease, key.operation_id, name="source_patch", data=patch.encode()
    )
    bundle = f.bind_contracts(config, source, construction, patch_ref)
    save_json(validation / "contracts.json", bundle.model_dump(mode="json"))
    comprehension = f.ComprehensionReview(
        status="pass", understood_outcome="Preserve the complete batch order and tails."
    )
    critic = f.VerifierCritique(
        status="pass",
        explanation="Independent protected observations cover the useful outcome.",
        challenge={"name": "challenge", "rationale": "Wrong ordering", "script": "false"},
        challenge_requirement_ids=["order"],
        independent_expectations=[
            {
                "requirement_id": "order",
                "input_case": "Three ordered batches",
                "expected_observation": "Original order is preserved",
                "provenance": {
                    "provenance": "public_math",
                    "explanation": "Fixed integer ordering",
                    "evidence_ids": ["instruction"],
                },
            }
        ],
    )
    for name, model in (("comprehension", comprehension), ("critic", critic)):
        save_json(
            validation / name / "artifact.json",
            {"input_digest": "d" * 64, "artifact": model.model_dump(mode="json")},
        )
        save_json(
            validation / name / "operation.json", {"input_digest": "d" * 64, "status": "completed"}
        )
    task_config, _contract, materialization = f._task_policy(task)
    cpus, memory = f._resources(task_config)
    profile = f.canonical_digest(
        {
            "provider": config.provider,
            "profile": "cpu-direct",
            "policy": 1,
            "cpus": cpus,
            "memory_mb": memory,
            "agent": task_config.environment.model_dump(mode="json"),
            "grader": task_config.verifier.environment.model_dump(mode="json"),
        }
    )
    budget = config.budget(source["id"])
    plan = f._requirements(value, critic, config)
    outcomes = {}
    for name, kind, expected, script, oracle, model in plan:
        folder = validation / "trials" / name
        output = folder / (name + "-retained")
        reward = expected if expected is not None else (0 if kind == "adversary" else 1)
        source_bytes = "# frozen observation " + ("base" if name == "baseline" else "changed")
        source_hash = hashlib.sha256(source_bytes.encode()).hexdigest()
        inventory = {
            "src/example/__init__.py": {"sha256": source_hash, "size_bytes": len(source_bytes)}
        }
        checksum = verify_collection(inventory, inventory)
        write(output / "artifacts/workspace/src/example/__init__.py", source_bytes)
        save_json(
            output / "artifacts/manifest.json",
            [
                {
                    "source": "/workspace/src/example",
                    "destination": "artifacts/workspace/src/example",
                    "status": "ok",
                }
            ],
        )
        save_json(output / "verifier/collection.json", inventory)
        save_json(folder / "pretransfer.json", inventory)
        save_json(
            output / "verifier/details.json",
            {
                "valid": True,
                "reward": reward,
                "outcome": "passed" if reward else "submission_failure",
                "n_tests": 3,
                "collection_digest": checksum,
                "origin": "/workspace/src/example/__init__.py",
            },
        )
        observation = {
            "value": name == "baseline",
            "package_origin": "/workspace/src/example/__init__.py",
            "origin_sha256": source_hash,
        }
        save_json(output / "verifier/source-observation.json", observation)
        if model:
            write(
                output / "agent/trace.jsonl",
                json.dumps(
                    {
                        "kind": "model",
                        "turn": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Retained complete model action.",
                        },
                        "cost_usd": 0,
                    }
                )
                + "\n",
            )
        resources = []
        for role in ("grader", "solver"):
            receipt = {
                "provider": config.provider,
                "role": role,
                "resource_id": name + role,
                "status": "stopped",
                "cleanup_confirmed": True,
                "deadline": deadline,
                "build_digest": f.BuildSpec.from_directory(
                    task / ("tests" if role == "grader" else "environment"), role=role
                ).digest,
            }
            resources.append(receipt)
            save_json(folder / "resources" / (role + ".json"), receipt)
        reservation = budget.reserve(0.01, "Retained mock cloud receipt")
        budget.settle(reservation, 0.01, estimated=True)
        save_json(
            folder / "reservation.json",
            {"id": reservation, "charged_usd": 0.01, "resources": resources},
        )
        outcome = f.TrialOutcome(
            kind=kind,
            task_digest=revision,
            path=str(output),
            provider=config.provider,
            profile_digest=profile,
            materialization_digest=f.canonical_digest(materialization),
            model=model,
            inference_digest=f.inference_digest(model, adversary=kind == "adversary")
            if model
            else None,
            observed_reward=reward,
            reward=reward,
            collection_verified=True,
            cleanup_confirmed=True,
            source_observation=observation,
            reservation_id=reservation,
            cloud_cost_estimated_usd=0.01,
        )
        save_json(folder / "outcome.json", outcome.model_dump())
        save_json(
            folder / "operation.json",
            {
                "task_digest": revision,
                "config_digest": config.digest(),
                "trial": output.name,
                "deadline": deadline,
                "kind": kind,
                "status": "completed",
                "outcome_path": str(folder / "outcome.json"),
            },
        )
        save_json(
            folder / "collection-receipt.json",
            {
                "task_digest": revision,
                "inventory_digest": checksum,
                "profile_digest": profile,
                "verified": True,
            },
        )
        kwargs = {
            "budget_path": str(config.ledger_path),
            "budget_limit": config.ledger_limit_usd,
            "budget_scope": "fixture:example-12",
            "scope_limit": config.candidate_limit_usd,
            "budget_group": config.campaign_id,
            "group_limit": config.campaign_limit_usd,
            "max_turns": config.solver_turns,
            "max_cost": config.solver_limit_usd,
            "mode": "adversary" if kind == "adversary" else ("solve" if model else "script"),
        }
        if script is not None:
            kwargs["script"] = script
        if oracle:
            kwargs["oracle_dir"] = str(task / "solution")
        save_json(
            folder / "harbor-config.json",
            {
                "task": {"path": str(task)},
                "trial_name": output.name,
                "trials_dir": str(folder),
                "environment": {"type": config.provider, "kwargs": {"absolute_deadline": deadline}},
                "agent": {"model_name": model, "kwargs": kwargs}
                if model or script is not None
                else {"name": "oracle"},
            },
        )
        outcomes[name] = outcome
    required = [
        f.TrialRequirement(id=n, kind=k, expected_reward=r, model=m).model_dump(mode="json")
        for n, k, r, _, _, m in plan
    ]
    save_json(
        validation / "trial-inventory.json",
        {"required": required, "outcomes": {n: o.model_dump() for n, o in outcomes.items()}},
    )
    witness = f.paired_witness(outcomes["baseline"], outcomes["oracle-0"])
    assert witness["passed"]
    save_json(validation / "source-witness.json", witness)
    for row in outcomes.values():
        row.materialization_verified = True
    texts = f._texts(task, construction, bundle, outcomes, witness, comprehension, critic)
    texts.update({"trial_" + n: f._trial_text(o) for n, o in outcomes.items()})
    for name, text in texts.items():
        journal.commit_artifact(lease, key.operation_id, name=name, data=text.encode())
    save_json(
        validation / "final-review/operation.json",
        {"status": "incomplete", "deadline": deadline, "charged_or_reserved_usd": 0.8},
    )
    write(
        validation / "final-review/trace.jsonl",
        json.dumps({"kind": "model", "cost_usd": 0.8}) + "\n",
    )
    reservation = budget.reserve(0.8, "Retained interrupted review")
    budget.settle(reservation, 0.8)
    journal.mark_uncertain(
        lease,
        key.operation_id,
        reason="BudgetExceeded during final review",
        receipt={"stage_root": str(parent_root)},
    )
    return SimpleNamespace(
        config=config,
        source=source,
        journal=journal,
        lease=lease,
        key=key,
        validation=validation,
        task=task,
        now=now,
        deadline=deadline,
        budget=budget,
    )


def prepare(s):
    return f.prepare_finalization(
        config=s.config,
        source=s.source,
        journal_path=s.journal.path,
        parent_operation_id=s.key.operation_id,
        validation_root=s.validation,
    )


def report(prepared, *, failed=False):
    criteria = {
        name: {
            "status": "pass",
            "score": 3,
            "explanation": "Complete captured evidence supports this dimension.",
            "evidence_ids": ["instruction"],
        }
        for name in QUALITY_DIMENSIONS
    }
    if failed:
        criteria["oracle_validity"].update(status="fail", score=1, severity="material")
    return QualityReport(
        pr=prepared.context.pr, revision_digest=prepared.context.revision_digest, criteria=criteria
    )


def test_prepare_reads_committed_artifacts_even_when_parent_record_is_empty(retained):
    s = retained
    parent = s.journal.get_operation(s.key.operation_id)
    ledger = s.config.ledger_path.read_bytes()
    assert not parent.artifacts
    prepared = prepare(s)
    assert len(prepared.evidence) == 14
    assert len(prepared.context.trials) == 11
    assert prepared.prior_review_charge_usd == 0.8
    assert prepared.deadline == s.deadline
    assert not f.admission_reasons(report(prepared), prepared.context)
    assert prepared.context.trials[0].outcome == "submission_failure"
    assert s.journal.get_operation(s.key.operation_id) == parent
    assert s.config.ledger_path.read_bytes() == ledger
    assert prepare(s).digest == prepared.digest


@pytest.mark.parametrize(
    "defect",
    [
        "task",
        "export",
        "cleanup",
        "missing",
        "reward",
        "profile",
        "budget",
        "trace",
        "inventory",
        "verdict",
    ],
)
def test_prepare_rejects_tampered_or_incomplete_evidence(retained, defect):
    s = retained
    trial = s.validation / "trials/solver-0"
    if defect == "task":
        write(s.task / "instruction.md", "tampered")
    elif defect == "export":
        write(trial / "solver-0-retained/artifacts/workspace/src/example/__init__.py", "tampered")
    elif defect == "cleanup":
        p = trial / "resources/solver.json"
        save_json(p, {**json.loads(p.read_text()), "cleanup_confirmed": False})
    elif defect == "missing":
        (trial / "collection-receipt.json").unlink()
    elif defect in {"reward", "profile"}:
        p = trial / "outcome.json"
        save_json(
            p,
            {
                **json.loads(p.read_text()),
                **({"reward": None} if defect == "reward" else {"profile_digest": "a" * 64}),
            },
        )
    elif defect == "budget":
        s.config = s.config.model_copy(update={"candidate_limit_usd": 21})
    elif defect == "trace":
        write(
            trial / "solver-0-retained/agent/trace.jsonl",
            json.dumps({"kind": "model", "finish_reason": "length"}) + "\n",
        )
    elif defect == "inventory":
        p = s.validation / "trial-inventory.json"
        value = json.loads(p.read_text())
        value["required"].pop()
        save_json(p, value)
    else:
        save_json(s.validation / "quality-report.json", {"status": "fail"})
    with pytest.raises((ValueError, FileNotFoundError)):
        prepare(s)


def test_prepare_rejects_active_other_child(retained):
    s = retained
    key = s.key.model_copy(update={"stage": "other", "effect": "other", "attempt": 0})
    s.journal.claim_operation(s.lease, key)
    with pytest.raises(f.FinalizationError, match="unresolved"):
        prepare(s)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_review_only_is_idempotent_and_never_changes_parent_or_budget_identity(
    retained, tmp_path, failed
):
    s = retained
    prepared = prepare(s)
    parent = s.journal.get_operation(s.key.operation_id)
    calls = []

    async def review_callback(**kwargs):
        calls.append(kwargs)
        assert kwargs["prior_review_charge_usd"] == 0.8
        assert kwargs["config"] == s.config
        assert kwargs["deadline"] == s.deadline
        assert kwargs["budget"].scope == s.budget.scope
        assert kwargs["evidence"] == prepared.evidence
        return report(prepared, failed=failed)

    args = dict(
        config=s.config,
        journal=s.journal,
        lease=s.lease,
        root=tmp_path / "fresh-review",
        review_callback=review_callback,
        review_policy={"fixture": 1},
    )
    result = await f.finalize_review_only(prepared, **args)
    assert result["status"] == ("needs_repair" if failed else "accepted")
    assert await f.finalize_review_only(prepared, **args) == result
    assert len(calls) == 1
    assert s.journal.get_operation(s.key.operation_id) == parent
    with sqlite3.connect(s.journal.path) as c:
        assert c.execute("SELECT COUNT(*) FROM selections").fetchone()[0] == (0 if failed else 1)
    with pytest.raises(ReconciliationRequired, match="differently bound"):
        await f.finalize_review_only(prepared, **{**args, "root": tmp_path / "reroll"})


@pytest.mark.asyncio
async def test_durable_result_recovers_selection_crash_without_another_model_call(
    retained, tmp_path, monkeypatch
):
    s = retained
    prepared = prepare(s)
    calls = []

    async def review_callback(**kwargs):
        calls.append(kwargs)
        return report(prepared)

    original = s.journal.select_revision

    def interrupted(*args, **kwargs):
        raise RuntimeError("crash after result commit")

    monkeypatch.setattr(s.journal, "select_revision", interrupted)
    args = dict(
        config=s.config,
        journal=s.journal,
        lease=s.lease,
        root=tmp_path / "fresh",
        review_callback=review_callback,
        review_policy={"test": 1},
    )
    with pytest.raises(RuntimeError, match="crash"):
        await f.finalize_review_only(prepared, **args)
    monkeypatch.setattr(s.journal, "select_revision", original)
    assert (await f.finalize_review_only(prepared, **args))["status"] == "accepted"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_original_deadline_and_uncertain_review_never_allow_a_fresh_call(retained, tmp_path):
    s = retained
    prepared = prepare(s)
    calls = []

    async def review_callback(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("request outcome uncertain")

    args = dict(
        config=s.config,
        journal=s.journal,
        lease=s.lease,
        root=tmp_path / "fresh",
        review_callback=review_callback,
        review_policy={"test": 1},
    )
    with pytest.raises(RuntimeError, match="uncertain"):
        await f.finalize_review_only(prepared, **args)
    with pytest.raises(ReconciliationRequired, match="never rerun"):
        await f.finalize_review_only(prepared, **args)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expired_original_deadline_cannot_dispatch(retained, tmp_path):
    s = retained
    prepared = prepare(s)
    s.now[0] = s.deadline + 1
    lease = s.journal.acquire_lease(s.lease.pr_id, owner="cleanup", ttl_seconds=60)

    async def forbidden(**kwargs):
        pytest.fail("No call after the original deadline")

    with pytest.raises(TimeoutError):
        await f.finalize_review_only(
            prepared,
            config=s.config,
            journal=s.journal,
            lease=lease,
            root=tmp_path / "expired",
            review_callback=forbidden,
            review_policy={"test": 1},
        )


def test_prepared_frozen_evidence_digest_detects_mutable_input_changes(retained):
    prepared = prepare(retained)
    assert replace(prepared, proof={**prepared.proof, "deadline": 9999}).digest != prepared.digest


@pytest.mark.asyncio
async def test_inline_delivery_is_committed_and_tampering_cannot_recover(retained, tmp_path):
    s = retained
    prepared = prepare(s)
    calls = []
    root = tmp_path / "inline"
    policy = {"delivery_policy": "complete-inline-final-review-v1"}

    async def callback(**kwargs):
        calls.append(kwargs)
        result = report(prepared)
        inputs = {
            "root": str(root),
            "policy": policy["delivery_policy"],
            "config": s.config.model_dump(mode="json"),
            "deadline": s.deadline,
            "prior_review_charge_usd": 0.8,
            "messages": [{"role": "user", "content": json.dumps(prepared.evidence)}],
        }
        identity = f.canonical_digest(inputs)
        files = {
            "inputs.json": inputs,
            "response.json": {"captured_report": result.model_dump(mode="json")},
            "delivery.json": {"input_digest": identity},
            "quality-report.json": result.model_dump(mode="json"),
        }
        for name, value in files.items():
            save_json(root / name, value)
        save_json(
            root / "operation.json",
            {
                "status": "completed",
                "input_digest": identity,
                "files": {
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files
                },
            },
        )
        return result

    args = dict(
        config=s.config,
        journal=s.journal,
        lease=s.lease,
        root=root,
        review_callback=callback,
        review_policy=policy,
    )
    result = await f.finalize_review_only(prepared, **args)
    operation = s.journal.get_operation(result["finalization_operation_id"])
    assert len(operation.artifacts) == 5
    receipt = json.loads(
        s.journal.verify_artifact(operation.artifacts["review_delivery"]).read_text()
    )
    assert receipt["files"]["inputs.json"]["text"] == (root / "inputs.json").read_text()
    assert await f.finalize_review_only(prepared, **args) == result
    assert len(calls) == 1
    write(root / "response.json", "{}")
    with pytest.raises(f.FinalizationError, match="file hash changed"):
        await f.finalize_review_only(prepared, **args)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_required_delivery_cannot_be_omitted_by_callback(retained, tmp_path):
    s = retained
    prepared = prepare(s)

    async def callback(**kwargs):
        return report(prepared)

    with pytest.raises(f.FinalizationError, match="delivery receipt is missing"):
        await f.finalize_review_only(
            prepared,
            config=s.config,
            journal=s.journal,
            lease=s.lease,
            root=tmp_path / "missing",
            review_callback=callback,
            review_policy={"delivery_policy": "complete-inline-final-review-v1"},
        )
    with sqlite3.connect(s.journal.path) as c:
        assert c.execute("SELECT COUNT(*) FROM selections").fetchone()[0] == 0
