from __future__ import annotations

import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import SpecificationPreflightReview, VerifierPreflightReview
from repo2rlenv.curation.protocol import DraftLimitExceeded, DraftTracker, MechanicalTracker
from repo2rlenv.curation.repair import (
    FinalJudgeOrigin,
    PreflightOrigin,
    RepairError,
    SeedRepair,
    prepare_seed_repair,
)
from repo2rlenv.curation.verifier_review import _snapshot
from tests.test_curation_repair import bound, canonical, write
from tests.test_curation_repair import family as retained_final_family


def prepare(f):
    return prepare_seed_repair(f.context, f.task, f.root, f.source, f.config, f.budget)


@pytest.fixture
def final_family(tmp_path):
    return retained_final_family.__wrapped__(tmp_path)


@pytest.fixture
def preflight_family(final_family):
    f = final_family
    ancestor_input = f.context.claim_context()
    with prepare(f):
        pass
    parent = f.root
    write(parent / "source.json", f.source)
    rows = json.loads((parent / "submitted-drafts.json").read_text())
    for number in (1, 3, 4):
        task = parent / f"drafts/{number}/task"
        shutil.copytree(f.task, task)
        (task / "instruction.md").write_text(f"Repair the documented boundary {number}.")
        write(task / "task.toml", {"version": "fixture"})
        (task / "tests").mkdir()
        (task / "tests/test_contract.py").write_text("def test_contract(): pass\n")
        rows.append({"digest": digest_task(task), "task": str(task)})
    write(parent / "submitted-drafts.json", rows)
    mechanics = json.loads((parent / "mechanical-submissions.json").read_text())
    mechanics.extend({"task": str(parent / f"bad/{i}"), "reason": "Missing plan"} for i in (1, 2))
    write(parent / "mechanical-submissions.json", mechanics)
    write(parent / "repair-progress.json", {"used_author_revisions": 3})
    traces = [write(parent / f"author-{i}.jsonl", {"kind": "model"}) for i in (1, 2)]
    key = f.budget.reserve(1, "retained preflight request")
    f.budget.settle(key, 0.75)
    entries = json.loads(f.budget.path.read_text())["entries"]
    retained = {k: v for k, v in entries.items() if v.get("scope") == f.budget.scope}
    receipt = {
        **f.receipt,
        "parent": f.receipt["child"],
        "child": {"scope": "next-scope", "scope_limit": 8, "group": "next-group", "group_limit": 8},
        "parent_entries_sha256": hashlib.sha256(canonical(retained)).hexdigest(),
        "lineage_scopes": [*f.receipt["lineage_scopes"], "next-scope"],
        "phase_groups": [*f.receipt["phase_groups"], "next-group"],
    }
    task = parent / "drafts/4/task"
    terminal = write(
        parent.parent / "terminal.json",
        {
            "status": "complete",
            "candidate_path": str(parent),
            "accepted": [],
            "budget_scope": f.budget.scope,
            "shared_ledger": str(f.budget.path),
            "rejected": [
                {
                    "id": f.source["id"],
                    "source": f.source["url"],
                    "status": "construction_failure",
                    "reasons": [
                        "BudgetExceeded: Candidate budget $8.00: $6.05 committed; need $1.98"
                    ],
                }
            ],
        },
    )
    context = f.context.model_copy(
        update={
            "parent_root": parent,
            "parent_task_digest": digest_task(task),
            "source": bound(parent / "source.json"),
            "design": bound(parent / "design.json"),
            "semantic_history": bound(parent / "submitted-drafts.json"),
            "mechanical_history": bound(parent / "mechanical-submissions.json"),
            "revision_history": bound(parent / "repair-progress.json"),
            "author_traces": traces,
            "review": None,
            "review_result": None,
            "audit": write(
                parent.parent / "new-audit.json",
                {
                    "task_digest": digest_task(task),
                    "finding": "Independent static counterexample; not executed.",
                },
            ),
            "budget_receipt": write(parent.parent / "next-budget.json", receipt),
        }
    )
    result = SimpleNamespace(
        **{
            **vars(f),
            "parent": parent,
            "task": task,
            "root": parent.parent / "grandchild",
            "context": context,
            "receipt": receipt,
            "budget": Budget(
                f.budget.path,
                380,
                scope="next-scope",
                scope_limit=8,
                group="next-group",
                group_limit=8,
            ),
            "terminal": terminal,
            "ancestor_input": ancestor_input,
        }
    )
    attach_preflight(result)
    return result


def attach_preflight(f, *, stage="verifier", status="error"):
    texts = (
        _snapshot(f.task)
        if stage == "verifier"
        else {
            name: (f.task / name).read_text()
            for name in ("instruction.md", "contract.json", "task.toml")
        }
    )
    identity = {
        "policy_version": 10 if stage == "verifier" else 3,
        "policy_sha256": "a" * 64,
        "inference": {"model": f.config.judge_model},
        "files": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in texts.items()},
    }
    folder = (
        f.parent.parent
        / (stage + "-reviews")
        / hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    )
    reads = {name: [] for name in texts}
    record = {"identity": identity, "status": status, "reads": reads, "charged_usd": 0.75}
    events = [
        {
            "kind": "input",
            "model": f.config.judge_model,
            "system": "Review static evidence.",
            "prompt": "Read the snapshot.",
        }
    ]
    state = None
    if status == "error":
        record.update(
            error_type="BudgetExceeded", error="Candidate budget $8.00: $6.05 committed; need $1.98"
        )
    else:
        review_type = (
            VerifierPreflightReview if stage == "verifier" else SpecificationPreflightReview
        )
        fields = {
            "score": 2,
            "blockers": ["The instruction permits an omitted helper."],
            "repairs": ["Collect the publicly permitted helper."],
            "optional_improvements": [],
            "evidence": ["instruction.md"],
        }
        if stage == "verifier":
            fields["authority_checks"] = [
                {
                    "requirement_id": requirement["id"],
                    "authoritative_input": None,
                    "competing_input": None,
                    "public_condition": "The documented edit boundary",
                    "discordant_fixture": None,
                    "expected_observation": None,
                    "conditional_shortcut": None,
                    "distinguishing_test": None,
                    "result": "not_applicable",
                    "reason": "The repair concerns artifact collection rather than competing inputs.",
                    "evidence": [{"path": "instruction.md", "quote": texts["instruction.md"]}],
                }
                for requirement in json.loads(texts["contract.json"])["requirements"]
            ]
        review = review_type.model_validate(fields)
        record["review"] = review.model_dump()
        terminal = json.loads(f.terminal.path.read_text())
        terminal["rejected"][0]["reasons"] = review.repairs
        f.terminal = write(f.terminal.path, terminal)
        calls, outputs = [], []
        for name, content in texts.items():
            identity_call = str(len(calls))
            calls.append(
                {
                    "id": identity_call,
                    "function": {
                        "name": "read_evidence",
                        "arguments": json.dumps({"path": name, "limit": 16000}),
                    },
                }
            )
            output = f"{name}: characters 0:{len(content)} of {len(content)}\n" + content
            outputs.append(
                {
                    "kind": "tool",
                    "name": "read_evidence",
                    "call_id": identity_call,
                    "output": output,
                }
            )
            reads[name] = [[0, len(content)]]
        assistant = {"role": "assistant", "tool_calls": calls}
        final = {"role": "assistant", "content": review.model_dump_json()}
        events.extend(
            [{"kind": "model", "message": assistant}, *outputs, {"kind": "model", "message": final}]
        )
        state = {
            "messages": [
                {"role": "system", "content": events[0]["system"]},
                {"role": "user", "content": events[0]["prompt"]},
                assistant,
                *[
                    {"role": "tool", "tool_call_id": row["call_id"], "content": row["output"]}
                    for row in outputs
                ],
                final,
            ],
            "turns": 2,
            "cost": 0.75,
        }
    if stage == "verifier":
        events.insert(0, {"kind": "verifier_review_started", "identity": identity})
        events.append({"kind": "verifier_review_finished", "status": status})
    input_bound = write(folder / "input.json", {"identity": identity, "texts": texts})
    result_bound = write(folder / "result.json", record)
    trace = folder / "trace.jsonl"
    trace.write_text("".join(json.dumps(event) + "\n" for event in events))
    origin = PreflightOrigin(
        stage=stage,
        input=input_bound,
        result=result_bound,
        trace=bound(trace),
        terminal=f.terminal,
        state=write(folder / "state.json", state) if state else None,
    )
    f.context = f.context.model_copy(update={"origin": origin})
    return origin


def change_bound(f, field, value):
    origin = f.context.origin
    f.context = f.context.model_copy(
        update={
            "origin": origin.model_copy(update={field: write(getattr(origin, field).path, value)})
        }
    )


@pytest.mark.parametrize("stage", ["verifier", "specification"])
@pytest.mark.parametrize("status", ["error", "completed"])
def test_preflight_origin_preserves_ancestor_history_without_fabricating_final_review(
    preflight_family, stage, status
):
    f = preflight_family
    attach_preflight(f, stage=stage, status=status)
    ancestor_bytes = (f.parent / "repair-input.json").read_bytes()
    entries_before = json.loads(f.budget.path.read_text())["entries"]
    with prepare(f) as repair:
        assert repair.used == 3
        assert repair.restore_task(f.task) == f.task
        drafts = DraftTracker(f.root / "submitted-drafts.json", 6)
        mechanics = MechanicalTracker(f.root / "mechanical-submissions.json", 6)
        assert len(drafts.rows) == 5 and len(mechanics.rows) == 3
        assert "Independent audit suggestions" in repair.feedback
        assert "Historical judge review" not in repair.feedback
        assert ("no completed semantic verdict" in repair.feedback) == (status == "error")
        assert not (f.root / "review.json").exists()
        assert not (f.root / "verdict.json").exists()
        repair.start_revision(3)
        drafts.observe("f" * 64, f.root / "drafts/new/task")
        with pytest.raises(DraftLimitExceeded):
            drafts.observe("e" * 64, f.root / "drafts/extra/task")
    assert (f.parent / "repair-input.json").read_bytes() == ancestor_bytes
    assert json.loads(f.budget.path.read_text())["entries"] == entries_before
    assert (
        SeedRepair.model_validate(json.loads(ancestor_bytes)["context"]).claim_context()
        == f.ancestor_input
    )
    with pytest.raises(RepairError, match="allowance exhausted"):
        with prepare(f):
            pass


@pytest.mark.parametrize(
    "damage",
    [
        "input_hash",
        "snapshot",
        "result_identity",
        "trace_hash",
        "trace_status",
        "missing_trace",
        "trace_symlink",
        "trace_oversized",
        "terminal_candidate",
        "terminal_error",
        "terminal_accepted",
        "unbound_state",
        "stale_audit",
        "fake_review",
        "running",
        "cost",
        "reads",
    ],
)
def test_invalid_preflight_origin_fails_before_claim_or_reservation(preflight_family, damage):
    f = preflight_family
    origin = f.context.origin
    before = f.budget.path.read_bytes()
    if damage == "input_hash":
        origin.input.path.write_text("{}")
    elif damage == "snapshot":
        data = json.loads(origin.input.path.read_text())
        data["texts"]["instruction.md"] = "Different draft"
        change_bound(f, "input", data)
    elif damage == "result_identity":
        data = json.loads(origin.result.path.read_text())
        data["identity"]["policy_version"] = 99
        change_bound(f, "result", data)
    elif damage == "trace_hash":
        origin.trace.path.write_text("{}\n")
    elif damage == "trace_status":
        lines = origin.trace.path.read_text().splitlines()
        lines[-1] = json.dumps({"kind": "verifier_review_finished", "status": "completed"})
        origin.trace.path.write_text("\n".join(lines))
        f.context = f.context.model_copy(
            update={"origin": origin.model_copy(update={"trace": bound(origin.trace.path)})}
        )
    elif damage == "missing_trace":
        origin.trace.path.unlink()
    elif damage == "trace_symlink":
        target = origin.trace.path.with_name("retained-trace.jsonl")
        origin.trace.path.rename(target)
        origin.trace.path.symlink_to(target)
    elif damage == "trace_oversized":
        origin.trace.path.write_bytes(b" " * 4_000_001)
        f.context = f.context.model_copy(
            update={"origin": origin.model_copy(update={"trace": bound(origin.trace.path)})}
        )
    elif damage.startswith("terminal_"):
        data = json.loads(origin.terminal.path.read_text())
        if damage == "terminal_candidate":
            data["candidate_path"] = str(f.parent.parent / "wrong")
        elif damage == "terminal_error":
            data["rejected"][0]["reasons"] = ["A different failure"]
        else:
            data["accepted"] = [{"id": f.source["id"]}]
        change_bound(f, "terminal", data)
    elif damage == "unbound_state":
        write(origin.result.path.parent / "state.json", {"messages": []})
    elif damage == "stale_audit":
        f.context = f.context.model_copy(
            update={"audit": write(f.context.audit.path, {"task_digest": "f" * 64})}
        )
    else:
        data = json.loads(origin.result.path.read_text())
        if damage == "fake_review":
            data["review"] = f.review.model_dump()
        elif damage == "running":
            data["status"] = "running"
        elif damage == "cost":
            data["charged_usd"] = -1
        else:
            data["reads"]["instruction.md"] = [[0, 999999]]
        change_bound(f, "result", data)
    with pytest.raises((RepairError, OSError)):
        with prepare(f):
            pytest.fail("Invalid origin must not claim a child")
    assert f.budget.path.read_bytes() == before
    assert not (f.parent / "repair-child.json").exists()
    assert not f.root.exists()


def test_typed_and_legacy_final_origins_roundtrip_without_ambiguous_input(final_family):
    f = final_family
    legacy = f.context.claim_context()
    assert "origin" not in legacy
    assert SeedRepair.model_validate(legacy).claim_context() == legacy
    typed = {k: v for k, v in legacy.items() if k not in {"review", "review_result"}}
    typed["origin"] = {
        "kind": "final_judge",
        "review": legacy["review"],
        "result": legacy["review_result"],
    }
    f.context = SeedRepair.model_validate(typed)
    assert isinstance(f.context.origin, FinalJudgeOrigin)
    with prepare(f) as repair:
        assert "Historical judge review" in repair.feedback
    with pytest.raises(ValidationError, match="mixed"):
        SeedRepair.model_validate({**legacy, "origin": typed["origin"]})
    with pytest.raises(ValidationError, match="either"):
        SeedRepair.model_validate({k: v for k, v in legacy.items() if k != "review"})


@pytest.mark.parametrize("damage", ["pass", "missing_state", "changed_final", "forged_reads"])
def test_completed_preflight_needs_real_rejected_result_and_read_journal(preflight_family, damage):
    f = preflight_family
    origin = attach_preflight(f, stage="specification", status="completed")
    if damage == "missing_state":
        origin.state.path.unlink()
        f.context = f.context.model_copy(
            update={"origin": origin.model_copy(update={"state": None})}
        )
    elif damage == "changed_final":
        state = json.loads(origin.state.path.read_text())
        state["messages"][-1]["content"] = "{}"
        change_bound(f, "state", state)
    elif damage == "forged_reads":
        lines = [json.loads(line) for line in origin.trace.path.read_text().splitlines()]
        lines = [line for line in lines if line.get("kind") != "tool"]
        origin.trace.path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        f.context = f.context.model_copy(
            update={"origin": origin.model_copy(update={"trace": bound(origin.trace.path)})}
        )
    else:
        record = json.loads(origin.result.path.read_text())
        record["review"].update(score=4, blockers=[], repairs=[])
        change_bound(f, "result", record)
    with pytest.raises(RepairError):
        with prepare(f):
            pass
    assert not f.root.exists()


def test_preflight_child_cannot_replay_parent_or_an_already_reviewed_digest(preflight_family):
    f = preflight_family
    with prepare(f) as repair:
        with pytest.raises(ValueError, match="unchanged parent"):
            repair.require_unreviewed_change(f.context.parent_task_digest)
        write(f.root / "revision-3/review.json", f.review.model_dump())
        write(f.root / "revision-3/evidence.json", {"task_digest": "e" * 64})
        with pytest.raises(ValueError, match="already has a final review"):
            repair.require_unreviewed_change("e" * 64)
    f.root = f.root.with_name("other-output")
    with pytest.raises(RepairError, match="different repair child"):
        with prepare(f):
            pass


def test_preflight_origin_cannot_hide_a_real_final_review(preflight_family):
    f = preflight_family
    folder = f.parent / "revision-2"
    shutil.copytree(f.task, folder / "task")
    write(folder / "review.json", f.review.model_dump())
    with pytest.raises(RepairError, match="final-judge origin"):
        with prepare(f):
            pass


@pytest.mark.parametrize("mismatch", ["digest", "path"])
def test_preflight_must_select_final_semantic_submission_before_claim(preflight_family, mismatch):
    f = preflight_family
    rows = json.loads(f.context.semantic_history.path.read_text())
    if mismatch == "digest":
        rows.append({"digest": "e" * 64, "task": str(f.parent / "drafts/later/task")})
    else:
        rows[-1]["task"] = str(f.parent / "revision-2/task")
    f.context = f.context.model_copy(
        update={"semantic_history": write(f.context.semantic_history.path, rows)}
    )
    before = f.budget.path.read_bytes()
    with pytest.raises(RepairError, match="latest submitted task"):
        with prepare(f):
            pass
    assert f.budget.path.read_bytes() == before
    assert not (f.parent / "repair-child.json").exists()


def test_completed_verifier_normalizes_optional_citation_lines_without_journal_edits(
    preflight_family,
):
    f = preflight_family
    origin = attach_preflight(f, status="completed")
    trace_before = origin.trace.path.read_bytes()
    record = json.loads(origin.result.path.read_text())
    for check in record["review"]["authority_checks"]:
        check["evidence"][0]["line"] = 1
    change_bound(f, "result", record)
    with prepare(f) as repair:
        assert "completed static preflight rejection" in repair.feedback
    assert origin.trace.path.read_bytes() == trace_before
    raw_final = json.loads(origin.state.path.read_text())["messages"][-1]["content"]
    assert json.loads(raw_final)["authority_checks"][0]["evidence"][0]["line"] is None


def test_completed_rejection_must_agree_with_terminal_failure(preflight_family):
    f = preflight_family
    origin = attach_preflight(f, stage="specification", status="completed")
    terminal = json.loads(origin.terminal.path.read_text())
    terminal["rejected"][0]["reasons"] = ["Unrelated infrastructure failure"]
    change_bound(f, "terminal", terminal)
    with pytest.raises(RepairError, match="rejection disagrees"):
        with prepare(f):
            pass
    assert not (f.parent / "repair-child.json").exists()


@pytest.mark.parametrize("stage", ["verifier", "specification"])
def test_completed_preflight_accepts_corrected_malformed_tool_input(preflight_family, stage):
    f = preflight_family
    origin = attach_preflight(f, stage=stage, status="completed")
    malformed = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "malformed-first-read",
                "function": {"name": "read_evidence", "arguments": '{"path":'},
            }
        ],
    }
    output = "Tool input error: Expecting value: line 1 column 9 (char 8)"
    events = [json.loads(line) for line in origin.trace.path.read_text().splitlines()]
    insertion = next(i for i, event in enumerate(events) if event.get("kind") == "model")
    events[insertion:insertion] = [
        {"kind": "model", "message": malformed},
        {
            "kind": "tool",
            "name": "read_evidence",
            "call_id": "malformed-first-read",
            "output": output,
        },
    ]
    origin.trace.path.write_text("".join(json.dumps(event) + "\n" for event in events))
    state = json.loads(origin.state.path.read_text())
    state["messages"][2:2] = [
        malformed,
        {"role": "tool", "tool_call_id": "malformed-first-read", "content": output},
    ]
    state["turns"] += 1
    f.context = f.context.model_copy(
        update={
            "origin": origin.model_copy(
                update={"trace": bound(origin.trace.path), "state": write(origin.state.path, state)}
            )
        }
    )
    trace_before = origin.trace.path.read_bytes()
    with prepare(f) as repair:
        assert "completed static preflight rejection" in repair.feedback
    assert origin.trace.path.read_bytes() == trace_before
