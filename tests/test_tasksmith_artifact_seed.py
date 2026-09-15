"""Seeded author revisions use existing validated stage input without paid calls."""

from __future__ import annotations

import json
import time
from copy import deepcopy

import pytest
from pydantic import BaseModel, ConfigDict

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.tasksmith.author import artifact
from repo2rlenv.tasksmith.author.budget import AuthorBudget


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    count: int
    tests: list[str]


@pytest.fixture
def options(tmp_path):
    seed = {"title": "preserve", "count": 0, "tests": ["large unchanged verifier"]}
    budget = RunBudget(BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10"), "seed", "5")
    return {
        "schema": Artifact,
        "stage": "design-1",
        "inputs": {"source": "fixed", "previous_design": seed, "other_design": deepcopy(seed)},
        "system": "Author the revised design.",
        "prompt": json.dumps({"previous_design": seed}),
        "root": tmp_path / "artifact",
        "budget": AuthorBudget(budget, tmp_path / "charges", "design-1"),
        "model": "anthropic/claude-sonnet-4-6",
        "runtime": "pi",
        "max_cost": 1,
        "max_turns": 4,
        "deadline": time.time() + 30,
        "initial_draft_key": "previous_design",
    }


@pytest.mark.asyncio
async def test_seeded_minimal_revision_preserves_original_and_requires_validation(
    monkeypatch, options
):
    original = deepcopy(options["inputs"])
    validated = []
    calls = 0
    root = options["root"]

    async def validate(value):
        validated.append(value.count)
        if value.count < 1:
            raise ValueError("count must be positive")

    async def agent(**kwargs):
        nonlocal calls
        calls += 1
        assert not validated
        assert not (root / "artifact.json").exists()
        seed = json.loads((root / "initial-draft.json").read_text())
        assert seed["status"] == "unvalidated" and seed["number"] == 0
        assert seed["input_key"] == "previous_design"
        assert seed["artifact"] == original["previous_design"]
        assert "inputs['previous_design']" in kwargs["prompt"]
        assert "Nothing is committed yet" in kwargs["prompt"]
        assert "committed" in await kwargs["handlers"]["revise_artifact"](patch={"count": 2})

    monkeypatch.setattr(artifact, "run_agent", agent)
    result = await artifact.artifact_stage(**options, validate=validate)
    assert result == Artifact(**{**original["previous_design"], "count": 2})
    assert options["inputs"] == original
    assert validated == [2]
    seed_bytes = (root / "initial-draft.json").read_bytes()
    draft = json.loads((root / "draft.json").read_text())
    committed = json.loads((root / "artifact.json").read_text())
    seed = json.loads(seed_bytes)
    assert draft["origin"] == "revise_artifact" and draft["number"] == 1
    assert seed["input_digest"] == draft["input_digest"] == committed["input_digest"]
    assert seed["artifact"] == original["previous_design"]
    assert await artifact.artifact_stage(**options, validate=validate) == result
    assert (root / "initial-draft.json").read_bytes() == seed_bytes
    assert calls == 1 and validated == [2]


@pytest.mark.asyncio
async def test_seed_revision_cannot_bypass_operator_validation(monkeypatch, options):
    checked = []

    async def validate(value):
        checked.append(value.count)
        if value.count == 0:
            raise ValueError("Seed still fails construction requirements")

    async def agent(**kwargs):
        response = await kwargs["handlers"]["revise_artifact"](patch={})
        assert "rejected" in response
        assert not (options["root"] / "artifact.json").exists()
        assert "committed" in await kwargs["handlers"]["revise_artifact"](patch={"count": 1})

    monkeypatch.setattr(artifact, "run_agent", agent)
    result = await artifact.artifact_stage(**options, validate=validate)
    assert result.count == 1
    assert checked == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [None, [], {"title": "incomplete"}, {"title": "x" * 512001}])
async def test_invalid_seed_fails_before_model_or_operation(monkeypatch, options, seed):
    async def forbidden(**kwargs):
        pytest.fail("Invalid seed must not reach the provider")

    monkeypatch.setattr(artifact, "run_agent", forbidden)
    options["inputs"]["previous_design"] = seed
    with pytest.raises(ValueError, match="invalid initial draft"):
        await artifact.artifact_stage(**options)
    assert options["budget"].spent == 0
    assert not options["root"].exists()


@pytest.mark.asyncio
async def test_seed_key_must_be_an_existing_controller_input(monkeypatch, options):
    async def forbidden(**kwargs):
        pytest.fail("Unknown seed input must not reach the provider")

    monkeypatch.setattr(artifact, "run_agent", forbidden)
    with pytest.raises(ValueError, match="existing input"):
        await artifact.artifact_stage(**{**options, "initial_draft_key": "unknown"})
    assert not options["root"].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["seed", "source", "seed_key", "disable_seed"])
async def test_completed_seeded_stage_rejects_changed_inputs(monkeypatch, options, change):
    calls = 0

    async def agent(**kwargs):
        nonlocal calls
        calls += 1
        await kwargs["handlers"]["revise_artifact"](patch={"count": 2})

    monkeypatch.setattr(artifact, "run_agent", agent)
    await artifact.artifact_stage(**options)
    root = options["root"]
    saved = {name: (root / name).read_bytes() for name in ("artifact.json", "initial-draft.json")}
    changed = {**options, "inputs": deepcopy(options["inputs"])}
    if change == "seed":
        changed["inputs"]["previous_design"]["count"] = 3
    elif change == "source":
        changed["inputs"]["source"] = "another source"
    else:
        changed["initial_draft_key"] = "other_design" if change == "seed_key" else None
    with pytest.raises(ValueError, match="different inputs"):
        await artifact.artifact_stage(**changed)
    assert calls == 1
    assert {name: (root / name).read_bytes() for name in saved} == saved


@pytest.mark.asyncio
async def test_seed_alone_is_not_accepted_and_incomplete_stage_cannot_replay(monkeypatch, options):
    calls = 0

    async def agent(**kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(artifact, "run_agent", agent)
    with pytest.raises(ValueError, match="without a validated artifact"):
        await artifact.artifact_stage(**options)
    seed = (options["root"] / "initial-draft.json").read_bytes()
    assert not (options["root"] / "artifact.json").exists()
    with pytest.raises(RuntimeError, match="reconciliation"):
        await artifact.artifact_stage(**options)
    assert (options["root"] / "initial-draft.json").read_bytes() == seed
    assert calls == 1


@pytest.mark.asyncio
async def test_orphaned_seed_is_not_overwritten(monkeypatch, options):
    async def forbidden(**kwargs):
        pytest.fail("Unreconciled seed must not reach the provider")

    monkeypatch.setattr(artifact, "run_agent", forbidden)
    root = options["root"]
    root.mkdir()
    (root / "initial-draft.json").write_text("preserved orphan")
    with pytest.raises(RuntimeError, match="reconciliation"):
        await artifact.artifact_stage(**options)
    assert (root / "initial-draft.json").read_text() == "preserved orphan"
    assert not (root / "operation.json").exists()


@pytest.mark.parametrize(
    "stage,previous,expected",
    [
        ("design-0", None, None),
        ("design-1", {"count": 1}, "previous_design"),
        ("investigate-1", {"count": 1}, None),
    ],
)
def test_runner_seeds_only_existing_design_input(monkeypatch, tmp_path, stage, previous, expected):
    from repo2rlenv.tasksmith import runner as module
    from repo2rlenv.tasksmith.models import Options

    BudgetLedger(tmp_path / "campaign/budget.sqlite3", limit_usd="10")
    tasksmith = module.Tasksmith(
        tmp_path / "run", tmp_path / "campaign", Options(), tmp_path / "wheel.whl"
    )
    sentinel = object()

    async def capture(**kwargs):
        assert kwargs["initial_draft_key"] == expected
        assert kwargs["inputs"]["previous_design"] is previous
        return sentinel

    monkeypatch.setattr(module, "artifact_stage", capture)
    assert (
        tasksmith.author(
            tmp_path / "candidate", stage, Artifact, {"previous_design": previous}, "/workspace"
        )
        is sentinel
    )
