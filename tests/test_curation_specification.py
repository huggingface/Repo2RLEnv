from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from repo2rlenv.curation import campaign
from repo2rlenv.curation.agent import IncompleteModelResponse
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.models import (
    CampaignConfig,
    SpecificationPreflightReview,
    SpecificationReview,
)
from repo2rlenv.curation.publish import evidence_snapshot

spec = importlib.import_module("repo2rlenv.curation.specification_review")


def result(*, score=4, blockers=None, repairs=None, optional_improvements=None):
    return SpecificationPreflightReview(
        score=score,
        blockers=blockers or [],
        repairs=repairs or [],
        optional_improvements=optional_improvements or [],
        evidence=[
            "instruction.md declares Widget.update; contract.json requires its observable state"
        ],
    )


def write_task(path, *, instruction="Widget.update accepts an integer and returns its new state."):
    path.mkdir(parents=True, exist_ok=True)
    (path / "instruction.md").write_text(instruction)
    (path / "contract.json").write_text('{"requirements": [{"behavior": "update state"}]}')


async def read_all(kwargs):
    read = kwargs["handlers"]["read_evidence"]
    for name in ("instruction.md", "contract.json"):
        offset = 0
        while True:
            page = await read(name, offset=offset)
            end, size = page.split("characters ")[1].split("\n")[0].split(" of ")
            offset = int(end.split(":")[1])
            if offset == int(size):
                break


def state(review=None, *, content=None, calls=None):
    return {
        "messages": [
            {
                "role": "assistant",
                "content": content or (review or result()).model_dump_json(),
                **({"tool_calls": calls} if calls else {}),
            }
        ],
        "turns": 2,
        "cost": 0.15,
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    task, root = tmp_path / "task", tmp_path / "candidate"
    write_task(task)
    budget = Budget(tmp_path / "budget.json", 20)

    async def judge(**kwargs):
        await read_all(kwargs)
        reserve = kwargs["budget"].reserve(0.2, "mock-specification-call")
        kwargs["budget"].settle(reserve, 0.15)
        return state()

    agent = AsyncMock(side_effect=judge)
    monkeypatch.setattr(spec, "run_agent", agent)
    return SimpleNamespace(task=task, root=root, budget=budget, agent=agent)


async def review(s, **kwargs):
    return await spec.review_specification(
        s.task, s.root, model=kwargs.get("model", "anthropic/claude-opus-5"), budget=s.budget
    )


def test_configuration_is_opt_in_and_cpu_first_enables_it():
    assert CampaignConfig().specification_review is False
    assert CampaignConfig().verifier_review is False
    config = CampaignConfig.model_validate_json(Path("configs/curation/cpu-first.json").read_text())
    assert config.specification_review is True
    assert config.verifier_review is True
    comparison = CampaignConfig.model_validate_json(
        Path("configs/curation/runtime-comparison.json").read_text()
    )
    assert comparison.specification_review is False
    assert comparison.verifier_review is False


@pytest.mark.parametrize(
    "score,blockers,expected",
    [(4, [], True), (3, [], True), (2, [], False), (4, ["recipe"], False)],
)
def test_specification_threshold(score, blockers, expected):
    # Retained historical records do not retroactively reinterpret mixed repair/polish lists.
    assert (
        SpecificationReview(
            score=score,
            blockers=blockers,
            repairs=["State outcomes instead"],
            evidence=["instruction.md describes the public update behavior"],
        ).passed
        is expected
    )


@pytest.mark.parametrize("score", [3, 4])
def test_current_preflight_required_repairs_block_even_with_high_score(score):
    assert not result(score=score, repairs=["State the tested input precedence explicitly"]).passed
    assert result(
        score=score, optional_improvements=["Shorten a redundant introductory sentence"]
    ).passed


def test_historical_shape_readable_but_not_a_current_preflight():
    historical = {
        "score": 3,
        "blockers": [],
        "repairs": ["Optional: shorten an introductory sentence"],
        "evidence": ["instruction.md identifies the public input and output"],
    }
    assert SpecificationReview.model_validate(historical).passed
    with pytest.raises(ValidationError, match="optional_improvements"):
        SpecificationPreflightReview.model_validate(historical)
    schema = SpecificationPreflightReview.model_json_schema()
    assert "optional_improvements" in schema["required"]
    assert "any entry prevents passing" in schema["properties"]["repairs"]["description"]


@pytest.mark.parametrize("optional", [[" "], None, "style"])
def test_current_optional_feedback_must_be_a_list_of_nonblank_entries(optional):
    with pytest.raises(ValidationError):
        SpecificationPreflightReview.model_validate(
            {**result().model_dump(), "optional_improvements": optional}
        )


@pytest.mark.parametrize(
    "update",
    [
        {"score": True},
        {"score": 5},
        {"score": 1},
        {"evidence": []},
        {"evidence": [" "]},
        {"blockers": ["recipe"]},
    ],
)
def test_incomplete_or_non_actionable_reviews_are_invalid(update):
    with pytest.raises(ValidationError):
        SpecificationPreflightReview.model_validate({**result().model_dump(), **update})


@pytest.mark.asyncio
async def test_bounded_independent_review_caches_by_specification_and_policy(setup, monkeypatch):
    s = setup
    assert (await review(s)).passed
    assert (await review(s)).passed
    assert s.agent.await_count == 1
    call = s.agent.call_args.kwargs
    assert call["model"] == "anthropic/claude-opus-5"
    assert call["budget"] is s.budget
    assert call["max_cost"] == 2
    assert call["max_turns"] == 6
    assert list(call["handlers"]) == ["read_evidence"]
    assert [t["function"]["name"] for t in call["tools"]] == ["read_evidence"]
    assert "Public interface signatures" in call["system"]
    assert "Treat material leakage as" in call["system"]
    assert "Any repair prevents passing regardless of the score" in call["system"]
    assert "do not turn those\ninto required corrections" in call["system"]
    with pytest.raises(ValueError, match="not a listed"):
        await call["handlers"]["read_evidence"]("../secret")
    artifact = json.loads(
        next((s.root / "specification-reviews").glob("*/result.json")).read_text()
    )
    assert artifact["status"] == "completed"
    assert artifact["cost_usd"] == artifact["charged_usd"] == 0.15
    assert artifact["identity"]["inference"]["model"] == call["model"]
    assert artifact["identity"]["policy_version"] == spec.POLICY_VERSION == 2
    assert next((s.root / "specification-reviews").glob("*/input.json")).is_file()
    assert next((s.root / "specification-reviews").glob("*/state.json")).is_file()
    (s.task / "tests").mkdir()
    (s.task / "tests/test_contract.py").write_text("modified tests, not specification")
    assert (await review(s)).passed
    assert s.agent.await_count == 1
    (s.task / "instruction.md").write_text("A revised public API contract.")
    await review(s)
    assert s.agent.await_count == 2
    (s.task / "contract.json").write_text('{"requirements": []}')
    await review(s)
    assert s.agent.await_count == 3
    await review(s, model="anthropic/claude-opus-4-6")
    assert s.agent.await_count == 4
    monkeypatch.setattr(spec, "SYSTEM", spec.SYSTEM + "\nUpdated policy.")
    await review(s)
    assert s.agent.await_count == 5


@pytest.mark.asyncio
async def test_policy_change_does_not_reuse_retained_previous_pass(setup, monkeypatch):
    s = setup
    current_policy = spec.POLICY_VERSION
    monkeypatch.setattr(spec, "POLICY_VERSION", current_policy - 1)
    assert (await review(s)).passed
    previous_path = next((s.root / "specification-reviews").glob("*/result.json"))
    previous = json.loads(previous_path.read_text())
    # Retained policy-1 shape allowed ambiguous mixed repair/polish lists.
    previous["review"].pop("optional_improvements")
    previous["review"]["repairs"] = ["Clarify an unstated graded requirement"]
    previous_path.write_text(json.dumps(previous))
    retained = previous_path.read_bytes()
    monkeypatch.setattr(spec, "POLICY_VERSION", current_policy)
    assert (await review(s)).passed
    assert s.agent.await_count == 2
    assert len(list((s.root / "specification-reviews").glob("*/result.json"))) == 2
    assert previous_path.read_bytes() == retained


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "blank", "wrong_type"])
async def test_malformed_current_cache_fails_without_reroll(setup, damage):
    s = setup
    assert (await review(s)).passed
    path = next((s.root / "specification-reviews").glob("*/result.json"))
    cached = json.loads(path.read_text())
    if damage == "missing":
        cached["review"].pop("optional_improvements")
    else:
        cached["review"]["optional_improvements"] = [" "] if damage == "blank" else None
    path.write_text(json.dumps(cached))
    retained = path.read_bytes()
    with pytest.raises(
        spec.SpecificationReviewError, match="Cached specification review unavailable"
    ):
        await review(s)
    assert s.agent.await_count == 1
    assert path.read_bytes() == retained


@pytest.mark.asyncio
async def test_current_model_omitting_classification_fails_without_reroll(setup):
    s = setup

    async def judge(**kwargs):
        await read_all(kwargs)
        old_shape = result().model_dump(exclude={"optional_improvements"})
        return state(content=json.dumps(old_shape))

    s.agent.side_effect = judge
    with pytest.raises(spec.SpecificationReviewError, match="optional_improvements"):
        await review(s)
    with pytest.raises(
        spec.SpecificationReviewError, match="Cached specification review unavailable"
    ):
        await review(s)
    assert s.agent.await_count == 1


@pytest.mark.asyncio
async def test_high_score_required_repair_is_cached_as_failure_without_reroll(setup):
    s = setup
    failed = result(
        score=3, repairs=["State which input determines matching when both are supplied"]
    )

    async def judge(**kwargs):
        await read_all(kwargs)
        return state(failed)

    s.agent.side_effect = judge
    assert await review(s) == failed
    assert not (await review(s)).passed
    assert s.agent.await_count == 1


@pytest.mark.asyncio
async def test_reader_is_bounded_and_uses_frozen_text(setup):
    s = setup
    text = "a" * (spec.MAX_PAGE_CHARS + 100)
    (s.task / "instruction.md").write_text(text)

    async def judge(**kwargs):
        (s.task / "instruction.md").write_text("changed after snapshot")
        read = kwargs["handlers"]["read_evidence"]
        page = await read("instruction.md", limit=10**9)
        assert page.partition("\n")[2] == text[: spec.MAX_PAGE_CHARS]
        await read_all(kwargs)
        return state()

    s.agent.side_effect = judge
    assert (await review(s)).passed
    saved = json.loads(next((s.root / "specification-reviews").glob("*/input.json")).read_text())
    assert saved["texts"]["instruction.md"] == text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unread", "gap", "malformed", "missing", "tool_calls"])
async def test_incomplete_results_never_pass_or_repeat_the_paid_call(setup, failure):
    s = setup

    async def judge(**kwargs):
        if failure == "gap":
            read = kwargs["handlers"]["read_evidence"]
            await read("instruction.md", offset=1)
            await read("contract.json")
        elif failure != "unread":
            await read_all(kwargs)
        if failure == "missing":
            return {"messages": [], "turns": 1, "cost": 0}
        return state(
            content="{" if failure == "malformed" else None,
            calls=[{"id": "pending"}] if failure == "tool_calls" else None,
        )

    s.agent.side_effect = judge
    with pytest.raises(spec.SpecificationReviewError, match="Incomplete specification review"):
        await review(s)
    with pytest.raises(
        spec.SpecificationReviewError, match="Cached specification review unavailable"
    ):
        await review(s)
    assert s.agent.await_count == 1
    saved = json.loads(next((s.root / "specification-reviews").glob("*/result.json")).read_text())
    assert saved["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["truncated", "budget", "cancelled"])
async def test_execution_errors_keep_evidence_and_propagate(setup, failure):
    s = setup
    error = {
        "truncated": IncompleteModelResponse("max_tokens", state(content="{")),
        "budget": BudgetExceeded("cap reached"),
        "cancelled": asyncio.CancelledError(),
    }[failure]
    s.agent.side_effect = error
    with pytest.raises(type(error)):
        await review(s)
    saved = json.loads(next((s.root / "specification-reviews").glob("*/result.json")).read_text())
    assert saved["status"] == "error"
    assert saved["error_type"] == type(error).__name__
    if failure == "truncated":
        assert next((s.root / "specification-reviews").glob("*/state.json")).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "symlink", "oversized", "binary", "empty"])
async def test_unavailable_input_never_calls_model_and_is_recorded(setup, failure):
    s = setup
    file = s.task / "instruction.md"
    file.unlink()
    if failure == "symlink":
        file.symlink_to(s.task / "contract.json")
    elif failure == "oversized":
        file.write_text("a" * (spec.MAX_FILE_BYTES + 1))
    elif failure == "binary":
        file.write_bytes(b"\xff")
    elif failure == "empty":
        file.write_text(" ")
    with pytest.raises(spec.SpecificationReviewError, match=r"Cannot review instruction\.md"):
        await review(s)
    s.agent.assert_not_awaited()
    assert (
        json.loads((s.root / "specification-reviews/input-error.json").read_text())["status"]
        == "error"
    )


@pytest.mark.asyncio
async def test_failed_review_is_cached_with_actionable_repairs(setup):
    s = setup
    failed = result(
        score=2,
        blockers=["instruction.md supplies an ordered algorithm"],
        repairs=["Replace implementation steps with observable input/output requirements"],
    )

    async def judge(**kwargs):
        await read_all(kwargs)
        return state(failed)

    s.agent.side_effect = judge
    assert await review(s) == failed
    assert await review(s) == failed
    assert s.agent.await_count == 1


@pytest.mark.asyncio
async def test_specification_evidence_survives_private_snapshot(setup):
    s = setup
    await review(s)
    with evidence_snapshot(s.root) as snapshot:
        assert next(snapshot.glob("specification-reviews/*/result.json")).is_file()
        assert next(snapshot.glob("specification-reviews/*/input.json")).is_file()
        assert next(snapshot.glob("specification-reviews/*/state.json")).is_file()
        assert not list(snapshot.glob("revision-*/specification-reviews"))


@pytest.fixture
def author_campaign(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    sandbox = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        prepare=AsyncMock(),
        shell=AsyncMock(),
        sandbox=SimpleNamespace(
            object_id="mock-sandbox",
            filesystem=SimpleNamespace(copy_from_local=SimpleNamespace(aio=AsyncMock())),
        ),
    )

    async def export(task):
        write_task(task)

    sandbox.export = AsyncMock(side_effect=export)
    monkeypatch.setattr(campaign, "AuthorSandbox", Mock(return_value=sandbox))
    monkeypatch.setattr(campaign, "finalize", Mock())
    preflight = AsyncMock(return_value=[])
    monkeypatch.setattr(campaign, "preflight", preflight)
    judge = AsyncMock(
        side_effect=AssertionError("Final review must still require execution evidence")
    )
    monkeypatch.setattr(campaign, "_review_revision", judge)
    source = {"id": "task", "url": "https://github.com/example/repo/pull/1"}
    return SimpleNamespace(
        root=root,
        source=source,
        sandbox=sandbox,
        preflight=preflight,
        judge=judge,
        budget=Budget(tmp_path / "budget.json", 20),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["specification", "verifier"])
@pytest.mark.parametrize("calls_tool", [False, True])
@pytest.mark.parametrize("passes", [False, True])
async def test_specification_precedes_cloud_with_or_without_author_tool(
    author_campaign, monkeypatch, calls_tool, passes, kind
):
    s = author_campaign
    static = (
        result()
        if passes
        else result(score=2, blockers=["Solution recipe"], repairs=["State observable outcomes"])
    )
    checks, feedback = [], []

    async def specification(task, root, *, model, budget):
        assert model == "anthropic/claude-opus-5"
        assert budget is s.budget
        checks.append(task)
        return static

    async def author(**kwargs):
        if calls_tool:
            feedback.append(await kwargs["handlers"]["validate_candidate"]())

    monkeypatch.setattr(campaign, f"review_{kind}", specification)
    monkeypatch.setattr(campaign, "run_agent", author)
    config = CampaignConfig(
        **{f"{kind}_review": True}, judge_model="anthropic/claude-opus-5", max_revisions=1
    )
    verdict = await campaign.curate_one(s.source, s.root, config, s.budget)
    assert len(checks) == (2 if calls_tool else 1)
    if passes:
        assert s.preflight.await_count >= 1
        assert verdict["status"] == "rejected"  # Static success cannot admit missing trials.
    else:
        s.preflight.assert_not_awaited()
        assert "State observable outcomes" in verdict["reasons"][1]
        if calls_tool:
            assert "State observable outcomes" in feedback[0]
    s.judge.assert_not_awaited()
    s.sandbox.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_config_does_not_add_a_model_call(author_campaign, monkeypatch):
    s = author_campaign
    specification = AsyncMock(side_effect=AssertionError("Opt-in only"))
    monkeypatch.setattr(campaign, "review_specification", specification)
    monkeypatch.setattr(campaign, "review_verifier", specification)
    monkeypatch.setattr(campaign, "run_agent", AsyncMock())
    await campaign.curate_one(s.source, s.root, CampaignConfig(max_revisions=1), s.budget)
    specification.assert_not_awaited()
    s.preflight.assert_awaited_once()


@pytest.mark.asyncio
async def test_score_three_required_repairs_reach_author_before_remote_validation(
    author_campaign, monkeypatch
):
    s = author_campaign
    repair = "Declare the required supplied-state authority when configuration disagrees"
    static = result(score=3, repairs=[repair])
    monkeypatch.setattr(campaign, "review_specification", AsyncMock(return_value=static))
    feedback = []

    async def author(**kwargs):
        feedback.append(await kwargs["handlers"]["validate_candidate"]())

    monkeypatch.setattr(campaign, "run_agent", author)
    verdict = await campaign.curate_one(
        s.source, s.root, CampaignConfig(specification_review=True, max_revisions=1), s.budget
    )
    assert repair in feedback[0]
    assert verdict["status"] == "rejected"
    assert repair in verdict["reasons"][1]
    s.preflight.assert_not_awaited()
    s.judge.assert_not_awaited()
    s.sandbox.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["specification", "verifier"])
@pytest.mark.parametrize("passes", [True, False])
async def test_resumed_checkpoint_is_reviewed_before_missing_trials(
    author_campaign, monkeypatch, passes, kind
):
    s = author_campaign
    seed = s.root.parent / "previous/revision-0/task"
    write_task(seed)
    accepted = {"status": "accepted", "score": 100}
    resumed = AsyncMock(return_value=accepted)
    monkeypatch.setattr(campaign, "_resume_validation", resumed)

    def prepare(*args):
        write_task(s.root / "revision-0/task")
        return (None, "digest", [], {})

    monkeypatch.setattr(campaign, "_prepare_pending_review", prepare)

    async def specification(task, root, **kwargs):
        assert root == s.root.parent  # Cache survives timestamped attempt changes.
        resumed.assert_not_awaited()
        if passes:
            return result()
        return result(score=2, blockers=["Leaked algorithm"], repairs=["Remove the recipe"])

    async def author(**kwargs):
        assert "Remove the recipe" in kwargs["prompt"]

    author = AsyncMock(side_effect=author)
    monkeypatch.setattr(campaign, "run_agent", author)
    monkeypatch.setattr(campaign, f"review_{kind}", specification)
    verdict = await campaign.curate_one(
        s.source,
        s.root,
        CampaignConfig(**{f"{kind}_review": True}, max_revisions=1),
        s.budget,
        seed,
    )
    if passes:
        assert verdict == accepted
        resumed.assert_awaited_once()
        author.assert_not_awaited()
        s.sandbox.start.assert_not_awaited()
    else:
        assert verdict["status"] == "rejected"
        resumed.assert_not_awaited()
        author.assert_awaited_once()
    s.preflight.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_resumed_preflight_cannot_start_cloud(author_campaign, monkeypatch):
    s = author_campaign
    seed = s.root.parent / "previous/revision-0/task"
    write_task(seed)
    monkeypatch.setattr(campaign, "_prepare_pending_review", Mock(return_value=(None, "d", [], {})))
    resumed = AsyncMock(side_effect=AssertionError("Cannot resume incomplete preflight"))
    monkeypatch.setattr(campaign, "_resume_validation", resumed)
    monkeypatch.setattr(
        campaign,
        "review_specification",
        AsyncMock(side_effect=spec.SpecificationReviewError("Incomplete JSON")),
    )
    with pytest.raises(spec.SpecificationReviewError, match="Incomplete JSON"):
        await campaign.curate_one(
            s.source, s.root, CampaignConfig(specification_review=True), s.budget, seed
        )
    resumed.assert_not_awaited()
    s.sandbox.start.assert_not_awaited()
    s.preflight.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["specification", "verifier"])
async def test_resume_without_trials_restores_preflight_repairs_before_author(
    author_campaign, monkeypatch, kind
):
    s = author_campaign
    seed = s.root.parent / "previous/drafts/1/task"
    write_task(seed)
    monkeypatch.setattr(campaign, "_prepare_pending_review", Mock(return_value=None))
    checked = []

    async def static(task, root, **kwargs):
        checked.append(task)
        return result(
            score=2,
            blockers=["Unit weights cannot distinguish incorrect weighting"],
            repairs=["Use unequal positive weights and assert the weighted result"],
        )

    async def author(**kwargs):
        assert checked == [seed]
        assert "Use unequal positive weights" in kwargs["prompt"]

    monkeypatch.setattr(campaign, f"review_{kind}", static)
    monkeypatch.setattr(campaign, "run_agent", AsyncMock(side_effect=author))
    verdict = await campaign.curate_one(
        s.source,
        s.root,
        CampaignConfig(**{f"{kind}_review": True}, max_revisions=1),
        s.budget,
        seed,
    )
    assert verdict["status"] == "rejected"
    assert checked[0] == seed
    s.preflight.assert_not_awaited()


@pytest.mark.asyncio
async def test_binary_verifier_cache_returns_filename_and_allows_author_repair(
    author_campaign, monkeypatch
):
    s = author_campaign
    verifier = importlib.import_module("repo2rlenv.curation.verifier_review")
    repaired = False
    filename = "tests/__pycache__/test_contract.cpython-312.pyc"

    async def export(task):
        write_task(task)
        (task / "tests").mkdir(exist_ok=True)
        (task / "tests/test_contract.py").write_text("def test_sum():\n    assert 2 + 2 == 4\n")
        if not repaired:
            cached = task / filename
            cached.parent.mkdir()
            cached.write_bytes(b"\xcb\x00\x00")

    async def reviewer(task, root, **kwargs):
        # Exercise the real input parser and its error class. Paid review is mocked.
        verifier._snapshot(task)
        return result()

    async def author(**kwargs):
        nonlocal repaired
        feedback = await kwargs["handlers"]["validate_candidate"]()
        assert filename in feedback
        assert "Repair the verifier review inputs" in feedback
        s.preflight.assert_not_awaited()
        repaired = True
        await kwargs["handlers"]["validate_candidate"]()
        s.preflight.assert_awaited_once()

    monkeypatch.setattr(s.sandbox, "export", AsyncMock(side_effect=export))
    monkeypatch.setattr(campaign, "review_verifier", reviewer)
    monkeypatch.setattr(campaign, "run_agent", author)
    verdict = await campaign.curate_one(
        s.source, s.root, CampaignConfig(verifier_review=True, max_revisions=1), s.budget
    )
    assert verdict["status"] == "rejected"  # Missing controls still prevent admission.
    s.sandbox.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_incomplete_verifier_call_is_not_reclassified_as_author_input(
    author_campaign, monkeypatch
):
    s = author_campaign
    verifier = importlib.import_module("repo2rlenv.curation.verifier_review")
    monkeypatch.setattr(campaign, "run_agent", AsyncMock())
    monkeypatch.setattr(
        campaign,
        "review_verifier",
        AsyncMock(side_effect=verifier.VerifierReviewError("Incomplete paid response")),
    )
    with pytest.raises(verifier.VerifierReviewError, match="Incomplete paid response"):
        await campaign.curate_one(
            s.source, s.root, CampaignConfig(verifier_review=True, max_revisions=1), s.budget
        )
    s.preflight.assert_not_awaited()
    s.sandbox.stop.assert_awaited_once()
