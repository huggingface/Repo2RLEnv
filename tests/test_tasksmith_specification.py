from __future__ import annotations

import asyncio
import json
import time

import pytest

from repo2rlenv.curation.budget import BudgetExceeded
from repo2rlenv.tasksmith import specification as s
from repo2rlenv.tasksmith import worker
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.review import ComprehensionReview

INITIAL = "Replace two guards with one combined check while retaining batch ordering."
CORRECTED = "Preserve ordered whole batches for both fixed and dynamic batch lengths."


@pytest.fixture
def args(tmp_path):
    config = TasksmithConfig(
        ledger_path=tmp_path / "ledger.json", ledger_limit_usd=100, campaign_id="spec-test"
    )
    return dict(
        config=config,
        budget=config.budget("pr1"),
        root=tmp_path / "spec",
        deadline=time.time() + 600,
        initial_text=INITIAL,
        visible_files=["src/example/batches.py", "README.md"],
    )


def verdict(status):
    return ComprehensionReview(
        status=status,
        understood_outcome="Preserve public whole-batch ordering behavior.",
        required_repairs=[
            {
                "explanation": "Describe observable behavior instead of specifying guard implementation.",
                "evidence_ids": ["instruction"],
            }
        ]
        if status == "fail"
        else [],
        advisory=[
            {
                "explanation": "Optional sentence polish is not required.",
                "evidence_ids": ["instruction"],
            }
        ],
    )


def install(monkeypatch, statuses, edits=(CORRECTED,)):
    reviews, calls = [], []

    async def review(**kwargs):
        reviews.append(kwargs)
        status = statuses[len(reviews) - 1]
        if isinstance(status, BaseException):
            raise status
        result = verdict(status)
        worker.save_json(kwargs["root"] / "mock-review.json", result.model_dump())
        return result

    async def run(**kwargs):
        calls.append(kwargs)
        instruction = edits[len(calls) - 1]
        reply = await kwargs["handlers"]["submit_artifact"](instruction=instruction)
        assert reply.startswith("Artifact committed")

    monkeypatch.setattr(s, "comprehension_review", review)
    monkeypatch.setattr(worker, "run_agent", run)
    return reviews, calls


def test_semantic_rejection_edits_public_text_then_reviews_before_return(args, monkeypatch):
    reviews, calls = install(monkeypatch, ["fail", "pass"])
    result = asyncio.run(s.approve_instruction(**args))
    assert result["instruction"] == CORRECTED
    assert [r["review"]["status"] for r in result["reviews"]] == ["fail", "pass"]
    assert len(calls) == 1
    call = calls[0]
    assert call["runtime"] == "langgraph" and call["model"] == args["config"].author_model
    assert set(call["handlers"]) == {"submit_artifact", "revise_artifact"}
    public = json.loads(call["prompt"])
    assert set(public) == {"instruction", "visible_files", "required_feedback"}
    assert public["instruction"] == INITIAL
    assert public["visible_files"] == sorted(args["visible_files"])
    assert "Optional sentence polish" not in call["prompt"]
    assert call["budget"] is args["budget"]
    assert (
        json.loads((args["root"] / "edits/0/operation.json").read_text())["deadline"]
        == args["deadline"]
    )
    assert call["max_turns"] == s.EDITOR_TURNS
    assert call["max_cost"] == args["config"].author_stage_limit_usd
    assert all(
        row["deadline"] == args["deadline"] and row["budget"] is args["budget"] for row in reviews
    )
    assert reviews[0]["root"] != reviews[1]["root"]
    assert args["budget"].spent == 0  # Mocked providers incur no charges.


def test_completed_approval_reuses_exact_text_without_reviewer_or_editor_reroll(args, monkeypatch):
    reviews, calls = install(monkeypatch, ["fail", "pass"])
    result = asyncio.run(s.approve_instruction(**args))
    before = {p: p.read_bytes() for p in args["root"].rglob("*") if p.is_file()}
    assert asyncio.run(s.approve_instruction(**args)) == result
    assert len(reviews) == 2 and len(calls) == 1
    assert {p: p.read_bytes() for p in before} == before


def test_initial_pass_performs_no_editor_call(args, monkeypatch):
    reviews, calls = install(monkeypatch, ["pass"])
    result = asyncio.run(s.approve_instruction(**args))
    assert result["instruction"] == INITIAL
    assert len(reviews) == 1 and calls == [] and result["edits"] == []


@pytest.mark.parametrize("change", ["text", "inventory", "scope"])
def test_changed_inputs_cannot_reuse_or_reroll_completed_root(args, monkeypatch, change):
    reviews, _ = install(monkeypatch, ["pass"])
    asyncio.run(s.approve_instruction(**args))
    if change == "text":
        args["initial_text"] = CORRECTED
    elif change == "inventory":
        args["visible_files"] = [*args["visible_files"], "new.py"]
    else:
        args["budget"] = args["config"].budget("another-pr")
    with pytest.raises(s.SpecificationApprovalError, match="different inputs"):
        asyncio.run(s.approve_instruction(**args))
    assert len(reviews) == 1


@pytest.mark.parametrize("target", ["approval", "review"])
def test_changed_retained_approval_or_review_evidence_fails_closed(args, monkeypatch, target):
    install(monkeypatch, ["pass"])
    result = asyncio.run(s.approve_instruction(**args))
    path = (
        args["root"] / "approval.json"
        if target == "approval"
        else args["root"]
        / next(name for name in result["evidence_files"] if name.endswith("mock-review.json"))
    )
    path.write_text("{}")
    with pytest.raises(s.SpecificationApprovalError, match="changed"):
        asyncio.run(s.approve_instruction(**args))


def test_exactly_two_corrections_then_operational_failure_preserves_all_verdicts(args, monkeypatch):
    reviews, calls = install(
        monkeypatch, ["fail", "fail", "fail"], [CORRECTED, CORRECTED + " Keep empty batches."]
    )
    with pytest.raises(s.SpecificationApprovalError, match="two public corrections"):
        asyncio.run(s.approve_instruction(**args))
    assert len(reviews) == 3 and len(calls) == 2
    assert not (args["root"] / "approval.json").exists()
    assert len(json.loads((args["root"] / "review-history.json").read_text())) == 3
    with pytest.raises(s.SpecificationApprovalError, match="reconciliation"):
        asyncio.run(s.approve_instruction(**args))
    assert len(reviews) == 3


@pytest.mark.parametrize(
    "status,exception",
    [
        ("incomplete", s.SpecificationApprovalError),
        (BudgetExceeded("scope exhausted"), BudgetExceeded),
        (TimeoutError("provider timeout"), TimeoutError),
        (ValueError("bad review artifact"), s.SpecificationApprovalError),
    ],
)
def test_incomplete_or_operational_review_failure_never_invokes_editor(
    args, monkeypatch, status, exception
):
    reviews, calls = install(monkeypatch, [status])
    with pytest.raises(exception):
        asyncio.run(s.approve_instruction(**args))
    assert len(reviews) == 1 and calls == []
    assert (
        json.loads((args["root"] / "approval-operation.json").read_text())["status"] == "incomplete"
    )


def test_full_inventory_bound_fails_before_models_without_truncation(args, monkeypatch):
    reviews, calls = install(monkeypatch, [])
    args["visible_files"] = [f"src/{i}-" + "x" * 100 + ".py" for i in range(5000)]
    with pytest.raises(s.SpecificationApprovalError, match="review bound"):
        asyncio.run(s.approve_instruction(**args))
    assert reviews == calls == []
    assert not args["root"].exists()


def test_expired_absolute_deadline_starts_no_work(args, monkeypatch):
    reviews, calls = install(monkeypatch, [])
    args["deadline"] = time.time() - 1
    with pytest.raises(TimeoutError):
        asyncio.run(s.approve_instruction(**args))
    assert reviews == calls == []


def test_unchanged_editor_output_cannot_be_approved_without_new_review(args, monkeypatch):
    reviews, calls = install(monkeypatch, ["fail"], [INITIAL])
    with pytest.raises(s.SpecificationApprovalError, match="changed public instruction"):
        asyncio.run(s.approve_instruction(**args))
    assert len(reviews) == len(calls) == 1


def test_editor_budget_failure_is_operational_and_cannot_resume(args, monkeypatch):
    reviews, _ = install(monkeypatch, ["fail"])

    async def exhausted(**kwargs):
        assert kwargs["budget"] is args["budget"]
        raise BudgetExceeded("Shared candidate cap reached")

    monkeypatch.setattr(worker, "run_agent", exhausted)
    with pytest.raises(BudgetExceeded, match="Shared candidate cap"):
        asyncio.run(s.approve_instruction(**args))
    assert len(reviews) == 1
    assert not (args["root"] / "approval.json").exists()
    with pytest.raises(s.SpecificationApprovalError, match="reconciliation"):
        asyncio.run(s.approve_instruction(**args))


def test_changed_instruction_exceeding_comprehension_bound_is_not_truncated(args, monkeypatch):
    calls = []

    async def review(**kwargs):
        calls.append(kwargs["instruction"])
        if len(kwargs["instruction"]) > 512000:
            raise ValueError("Public comprehension evidence exceeds review bound")
        return verdict("fail")

    async def editor(**kwargs):
        return s.PublicInstruction(instruction="x" * 513000)

    monkeypatch.setattr(s, "comprehension_review", review)
    monkeypatch.setattr(s, "artifact_stage", editor)
    with pytest.raises(s.SpecificationApprovalError, match="review bound"):
        asyncio.run(s.approve_instruction(**args))
    assert calls == [INITIAL, "x" * 513000]
    assert not (args["root"] / "approval.json").exists()


def test_specification_accepts_complete_45000_character_inventory(args, monkeypatch):
    reviews, calls = install(monkeypatch, ["pass"])
    files = [f"src/example/module_{i:04d}_with_complete_public_filename.py" for i in range(900)]
    assert len(json.dumps(files)) > 45000
    args["visible_files"] = files
    result = asyncio.run(s.approve_instruction(**args))
    assert result["instruction"] == INITIAL
    assert reviews[0]["visible_files"] == sorted(files)
    assert calls == []
