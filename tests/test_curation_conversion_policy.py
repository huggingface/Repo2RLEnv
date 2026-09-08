"""Conversion policy integration: real local gates, mocked remote/model boundaries."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from repo2rlenv.curation import campaign, design
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import CampaignConfig, SpecificationReview, VerifierReview
from repo2rlenv.curation.protocol import DraftLimitExceeded, MechanicalLimitExceeded


def plan():
    return {
        "behaviors": [
            {
                "requirement": name,
                "expected_result": "Derive expected output from fixed independent inputs covering empty and nonempty cases.",
                "tests": ["test_" + name],
                "mutations": ["wrong_" + name],
                "equivalents": ["alternative"],
            }
            for name in ("empty", "nested")
        ],
        "offline_dependencies": "Pinned standard Python and pytest==8.4.2; all fixtures are local.",
        "artifact_boundary": "Editable src/widget includes public behavior and helper modules.",
    }


def task_files():
    return {
        "instruction.md": "Fix the widget behavior in src/widget, preserving empty and nested input behavior.",
        "environment/Dockerfile": "FROM python:3.12-slim@sha256:"
        + "a" * 64
        + "\nWORKDIR /workspace\nRUN curl "
        + "b" * 40
        + "\nRUN pip install pytest==8.4.2\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_contract.py": "from probe import run_probe\ndef test_empty(): run_probe('print(0)')\ndef test_nested(): pass\ndef test_general(): pass\n",
        "contract.json": json.dumps(
            {
                "title": "Widget",
                "rationale": "Exercise public empty and nested behavior",
                "source_paths": ["src/widget"],
                "requirements": [
                    {"id": n, "behavior": n + " input behavior", "tests": ["test_" + n]}
                    for n in ("empty", "nested")
                ],
                "mutations": [
                    {
                        "name": "wrong_" + n,
                        "rationale": "Break " + n + " behavior",
                        "script": "true",
                    }
                    for n in ("empty", "nested")
                ],
                "equivalents": [
                    {
                        "name": "alternative",
                        "rationale": "Equivalent representation",
                        "script": "true",
                    }
                ],
                "min_tests": 3,
            }
        ),
        "verification-plan.json": json.dumps(plan()),
    }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    source = {
        "id": "test-pr",
        "url": "https://github.com/a/b/pull/1",
        "repo": "a/b",
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
    }
    budget = Budget(tmp_path / "shared-ledger.json", 40, scope="conversion", scope_limit=8)
    root = tmp_path / "candidate"
    h = SimpleNamespace(
        root=root, source=source, budget=budget, exports=[], review_paths=[], events=[], author=None
    )

    class Sandbox:
        def __init__(self, timeout):
            self.sandbox = SimpleNamespace(object_id="mock-remote")

        async def start(self):
            h.events.append("start")

        async def prepare(self, source):
            h.events.append("prepare")

        async def stop(self):
            h.events.append("stop")

        async def shell(self, command, timeout_sec=120):
            return json.dumps({"exit_code": 0, "stdout": "", "stderr": ""})

        async def write(self, path, text):
            assert (root / "design.json").is_file()
            h.events.append("plan_prepared")

        async def export(self, destination):
            files = h.exports.pop(0)
            destination.mkdir(parents=True, exist_ok=True)
            if isinstance(files, Exception):
                raise files
            for name, content in files.items():
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

    async def planner(**kwargs):
        assert kwargs["budget"] is budget
        h.events.append("planning")
        await kwargs["handlers"]["submit_design"](
            task_request="Fix the widget's public empty and nested behavior while retaining independent expected outputs.",
            verification_plan=plan(),
        )

    async def author(**kwargs):
        assert kwargs["budget"] is budget
        assert (root / "design.json").exists()
        h.events.append("implementation")
        return await h.author(kwargs["handlers"]["validate_candidate"])

    async def specification(task, review_root, **kwargs):
        assert kwargs["budget"] is budget
        h.review_paths.append(task)
        # These represent provider results, not a mocked structural gate.
        reservation = budget.reserve(0.02, "mock-static-review")
        budget.settle(reservation, 0.02)
        return SpecificationReview(
            score=3,
            blockers=[],
            repairs=[],
            evidence=["The public task request specifies the intended behavior."],
        )

    async def verifier(task, review_root, **kwargs):
        assert kwargs["budget"] is budget
        return VerifierReview(
            score=2,
            blockers=["Independent behavior coverage is incomplete."],
            repairs=["Add the missing distinguishing behavioral assertion."],
            evidence=["The current complete test source lacks a distinguishing case."],
        )

    monkeypatch.setattr(campaign, "AuthorSandbox", Sandbox)
    monkeypatch.setattr(design, "run_agent", planner)
    monkeypatch.setattr(campaign, "run_agent", author)
    monkeypatch.setattr(campaign, "review_specification", specification)
    monkeypatch.setattr(campaign, "review_verifier", verifier)
    h.preflight = AsyncMock(
        side_effect=AssertionError("Static rejection must precede paid remote trials")
    )
    monkeypatch.setattr(campaign, "preflight", h.preflight)

    async def run(**overrides):
        config = {
            "submission_policy": "conversion",
            "max_candidate_drafts": 2,
            "max_mechanical_submissions": 6,
            "require_verification_plan": True,
            "specification_review": True,
            "verifier_review": True,
        }
        config.update(overrides)
        return await campaign.curate_one(source, root, CampaignConfig(**config), budget)

    h.run = run
    h.mechanical = lambda: json.loads((root / "mechanical-submissions.json").read_text())
    h.semantic = lambda: (
        json.loads((root / "submitted-drafts.json").read_text())
        if (root / "submitted-drafts.json").exists()
        else []
    )
    return h


@pytest.mark.asyncio
async def test_repeated_structural_errors_are_mechanical_then_last_semantic_failure_stops(harness):
    pytest.importorskip("harbor")
    h = harness
    invalid = task_files()
    del invalid["solution/solve.sh"]
    h.exports = [copy.deepcopy(invalid), copy.deepcopy(invalid), task_files()]

    async def author(validate):
        for _ in range(2):
            assert "Structural" in await validate()
            assert h.semantic() == []
        assert len(h.mechanical()) == 2
        await validate()
        pytest.fail("The first failed complete task exhausts the configured semantic limit")

    h.author = author
    with pytest.raises(DraftLimitExceeded) as exc:
        await h.run(max_candidate_drafts=1)
    assert not isinstance(exc.value, MechanicalLimitExceeded)
    assert len(h.mechanical()) == 2
    assert len(h.semantic()) == 1
    assert len(h.review_paths) == 1
    assert "coverage" in (h.root / "repair-limit-feedback.json").read_text()
    assert h.events[-1] == "stop"
    h.preflight.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_distinct_semantic_failure_stops_before_third_repair(harness):
    pytest.importorskip("harbor")
    h = harness
    revised = task_files()
    revised["instruction.md"] += " Preserve stable ordering too."
    h.exports = [task_files(), revised]

    async def author(validate):
        assert "verifier preflight" in await validate()
        assert len(h.semantic()) == 1
        await validate()
        pytest.fail("Second semantic failure must terminate immediately")

    h.author = author
    with pytest.raises(DraftLimitExceeded):
        await h.run()
    assert len(h.semantic()) == 2
    assert len(h.review_paths) == 2
    assert not (h.root / "mechanical-submissions.json").exists()
    assert not h.exports


@pytest.mark.asyncio
async def test_missing_plan_restored_exactly_from_accepted_design(harness):
    pytest.importorskip("harbor")
    h = harness
    files = task_files()
    del files["verification-plan.json"]
    h.exports = [files]

    async def author(validate):
        assert "verifier preflight" in await validate()
        assert len(h.semantic()) == 1
        exported = h.root / "drafts/1/task/verification-plan.json"
        assert json.loads(exported.read_text()) == plan()
        raise campaign.CandidateDeferred("End mocked author after restoration observation")

    h.author = author
    with pytest.raises(campaign.CandidateDeferred):
        await h.run()
    assert len(h.review_paths) == 1
    assert not (h.root / "mechanical-submissions.json").exists()


@pytest.mark.asyncio
async def test_unknown_plan_mapping_is_not_invented_or_sent_to_paid_review(harness):
    pytest.importorskip("harbor")
    h = harness
    files = task_files()
    unknown = plan()
    unknown["behaviors"][0]["mutations"] = ["unprovided_control"]
    files["verification-plan.json"] = json.dumps(unknown)
    h.exports = [files]

    async def author(validate):
        assert "missing negative control" in await validate()
        assert h.semantic() == []
        exported = h.root / "drafts/1/task/verification-plan.json"
        assert json.loads(exported.read_text()) == unknown
        raise campaign.CandidateDeferred("End mocked author after invalid mapping observation")

    h.author = author
    with pytest.raises(campaign.CandidateDeferred):
        await h.run()
    assert len(h.mechanical()) == 1
    assert not h.review_paths
    h.preflight.assert_not_awaited()


@pytest.mark.asyncio
async def test_mechanical_cap_terminates_even_identical_invalid_exports(harness):
    h = harness
    files = task_files()
    del files["instruction.md"]
    h.exports = [copy.deepcopy(files), copy.deepcopy(files)]

    async def author(validate):
        assert "Structural" in await validate()
        await validate()
        pytest.fail("Mechanical allowance must stop the second invalid export")

    h.author = author
    with pytest.raises(MechanicalLimitExceeded):
        await h.run(max_mechanical_submissions=2)
    assert len(h.mechanical()) == 2
    assert h.semantic() == []
    assert not h.review_paths
    assert h.events[-1] == "stop"
    h.preflight.assert_not_awaited()


@pytest.mark.parametrize(
    "override",
    [
        {"max_candidate_drafts": None},
        {"require_verification_plan": False},
        {"specification_review": False},
        {"verifier_review": False},
    ],
)
def test_conversion_requires_bounded_semantics_plan_and_both_reviews(override):
    values = dict(
        submission_policy="conversion",
        max_candidate_drafts=2,
        require_verification_plan=True,
        specification_review=True,
        verifier_review=True,
    )
    values.update(override)
    with pytest.raises(ValueError, match="Conversion policy"):
        CampaignConfig(**values)


@pytest.mark.asyncio
async def test_export_failure_counts_once_without_semantic_or_review(harness):
    h = harness
    h.exports = [ValueError("Mock remote export rejects a symlink")]

    async def author(validate):
        assert "export rejects a symlink" in await validate()
        assert h.semantic() == []
        raise campaign.CandidateDeferred("End after invalid export observation")

    h.author = author
    with pytest.raises(campaign.CandidateDeferred):
        await h.run()
    assert len(h.mechanical()) == 1
    assert not h.review_paths
    h.preflight.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_review_fixture_is_mechanical_before_semantic_count(harness):
    pytest.importorskip("harbor")
    h = harness
    files = task_files()
    # An arbitrary binary helper must not be hidden by cache sanitation.
    files["tests/fixture.bin"] = "\x00not reviewable source"
    h.exports = [files]

    async def author(validate):
        assert "binary evidence" in await validate()
        assert h.semantic() == []
        assert (h.root / "drafts/1/task/tests/fixture.bin").read_text() == files[
            "tests/fixture.bin"
        ]
        raise campaign.CandidateDeferred("End after review input failure observation")

    h.author = author
    with pytest.raises(campaign.CandidateDeferred):
        await h.run()
    assert len(h.mechanical()) == 1
    assert not h.review_paths
    h.preflight.assert_not_awaited()


@pytest.mark.asyncio
async def test_restored_plan_cannot_invent_mapping_for_a_changed_contract(harness):
    pytest.importorskip("harbor")
    h = harness
    files = task_files()
    del files["verification-plan.json"]
    contract = json.loads(files["contract.json"])
    contract["requirements"][1]["id"] = "unknown_requirement"
    files["contract.json"] = json.dumps(contract)
    h.exports = [files]

    async def author(validate):
        assert "every requirement exactly once" in await validate()
        assert h.semantic() == []
        restored = json.loads((h.root / "drafts/1/task/verification-plan.json").read_text())
        assert restored == plan()
        assert all(b["requirement"] != "unknown_requirement" for b in restored["behaviors"])
        raise campaign.CandidateDeferred("End after unmatched contract observation")

    h.author = author
    with pytest.raises(campaign.CandidateDeferred):
        await h.run()
    assert len(h.mechanical()) == 1
    assert not h.review_paths


def pilot_protocol(tmp_path, *, conversion=False):
    from repo2rlenv.curation import pilot

    ledger = tmp_path / "pilot-shared-ledger.json"
    Budget(ledger, 380).reserve(1, "retained prior campaign")
    settings = dict(
        target=5,
        budget_usd=40,
        max_candidate_usd=8,
        max_revisions=2,
        max_candidate_drafts=2,
        require_verification_plan=True,
        specification_review=True,
        verifier_review=True,
    )
    if conversion:
        settings.update(
            submission_policy="conversion",
            acceptance_policy="validity",
            max_revisions=4,
            max_candidate_drafts=3,
            max_mechanical_submissions=6,
        )
    return {
        "id": "conversion-protocol-test",
        "runtime_digest": pilot.runtime_digest(),
        "production_limit_usd": 380,
        "ledger": str(ledger),
        "config": CampaignConfig(**settings).model_dump(),
        "sources": [
            dict(
                url=f"https://github.com/a/b/pull/{i}",
                repo="a/b",
                id=f"a-b-{i}",
                base_sha="a" * 40,
                head_sha="b" * 40,
            )
            for i in range(1, 6)
        ],
    }


def test_frozen_legacy_pilot_rejects_three_drafts(tmp_path):
    from repo2rlenv.curation import pilot

    protocol = pilot_protocol(tmp_path)
    assert pilot.validate_protocol(protocol).max_candidate_drafts == 2
    protocol["config"]["max_candidate_drafts"] = 3
    with pytest.raises(ValueError, match="two drafts for legacy"):
        pilot.validate_protocol(protocol)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_revisions", 3),
        ("max_candidate_drafts", 2),
        ("max_mechanical_submissions", 5),
        ("acceptance_policy", "legacy"),
        ("budget_usd", 41),
        ("max_candidate_usd", 9),
    ],
)
def test_conversion_pilot_requires_exact_frozen_allowances(tmp_path, field, value):
    from repo2rlenv.curation import pilot

    protocol = pilot_protocol(tmp_path, conversion=True)
    config = pilot.validate_protocol(protocol)
    assert (
        config.max_revisions,
        config.max_candidate_drafts,
        config.max_mechanical_submissions,
    ) == (4, 3, 6)
    assert (config.budget_usd, config.max_candidate_usd, config.acceptance_policy) == (
        40,
        8,
        "validity",
    )
    protocol["config"][field] = value
    with pytest.raises(ValueError):
        pilot.validate_protocol(protocol)


@pytest.mark.asyncio
async def test_pilot_classifies_mechanical_limit_without_marking_source_unsuitable(
    tmp_path, monkeypatch
):
    from repo2rlenv.curation import pilot

    protocol = pilot_protocol(tmp_path, conversion=True)
    path = tmp_path / "protocol-input.json"
    path.write_text(json.dumps(protocol))
    calls = []

    async def curate(source, root, config, budget):
        assert config.submission_policy == "conversion"
        assert budget.path == tmp_path / "pilot-shared-ledger.json"
        assert (budget.scope_limit, budget.group_limit) == (8, 40)
        calls.append(source["url"])
        budget.reserve(0.1, "mock unsettled author work")
        raise MechanicalLimitExceeded("six failed mechanical inputs")

    monkeypatch.setattr(pilot, "curate_one", curate)
    result = await pilot.run_pilot(path, tmp_path / "pilot")
    assert len(result["rows"]) == 5
    assert {r["status"] for r in result["rows"]} == {"mechanical_limit"}
    assert all(r["source_unsuitability_established"] is False for r in result["rows"])
    assert result["charged_or_reserved_usd"] == pytest.approx(0.5)
    await pilot.run_pilot(path, tmp_path / "pilot")
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_shared_pilot_claim_forbids_mode_switch_even_with_new_output(tmp_path, monkeypatch):
    from repo2rlenv.curation import pilot

    protocol = pilot_protocol(tmp_path)
    path = tmp_path / "protocol-input.json"
    path.write_text(json.dumps(protocol))
    calls = []

    async def curate(source, root, config, budget):
        calls.append(source["url"])
        budget.reserve(0.1, "mock retained author work")
        raise DraftLimitExceeded("legacy two-submission allowance exhausted")

    monkeypatch.setattr(pilot, "curate_one", curate)
    out = tmp_path / "original-pilot"
    old = await pilot.run_pilot(path, out)
    assert {r["status"] for r in old["rows"]} == {"repair_limit"}
    before = (out / "manifest.json").read_bytes()
    protocol["config"].update(
        submission_policy="conversion",
        acceptance_policy="validity",
        max_revisions=4,
        max_candidate_drafts=3,
        max_mechanical_submissions=6,
    )
    # The new policy is valid as a fresh protocol, but cannot reuse this group's claim.
    pilot.validate_protocol(protocol)
    path.write_text(json.dumps(protocol))
    for destination in (out, tmp_path / "different-output"):
        with pytest.raises(ValueError, match="original output directory and protocol"):
            await pilot.run_pilot(path, destination)
    assert len(calls) == 5
    assert (out / "manifest.json").read_bytes() == before


@pytest.mark.asyncio
async def test_accounting_write_failure_still_stops_cloud_and_settles_reservation(
    harness, monkeypatch
):
    h = harness
    files = task_files()
    del files["instruction.md"]
    h.exports = [files]
    original_save = campaign.save

    def fail_accounting(path, data):
        if path.name == "construction-accounting.json":
            raise OSError("mock accounting volume full")
        return original_save(path, data)

    async def author(validate):
        assert "Structural" in await validate()
        raise campaign.CandidateDeferred("End before injected accounting failure")

    h.author = author
    monkeypatch.setattr(campaign, "save", fail_accounting)
    with pytest.raises(OSError, match="accounting volume full"):
        await h.run()
    assert h.events[-1] == "stop"
    ledger = json.loads(h.budget.path.read_text())
    cloud = [e for e in ledger["entries"].values() if e["label"] == "cloud:author:test-pr"]
    assert len(cloud) == 1
    assert cloud[0]["status"] == "estimated"
    assert 0 < cloud[0]["charged_usd"] < cloud[0]["reserved_usd"]
