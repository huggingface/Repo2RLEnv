from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo2rlenv.curation import campaign
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.models import CampaignConfig, TrialEvidence
from tests.test_curation_repair import family as retained_repair_family
from tests.test_curation_repair import write


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    state = SimpleNamespace(
        clock=0.0,
        events=[],
        sandboxes=[],
        author_inputs=[],
        preflights=[],
        full_trials=[],
        fail_control=False,
        invalid=None,
        stop_error=None,
        callback=False,
        bad_export=False,
        active_seconds=20.0,
        validation_seconds=100.0,
        change_after_callback=False,
        model_budget_error=False,
        trial_ceiling=None,
        mutate_during_stop=False,
    )
    monkeypatch.setattr(campaign, "time", SimpleNamespace(monotonic=lambda: state.clock))
    state.root = tmp_path / "candidate"
    state.source = {"id": "example-1", "url": "https://example.test/pull/1"}
    state.budget = Budget(
        tmp_path / "ledger.json",
        40,
        scope="candidate",
        scope_limit=8,
        group="phase",
        group_limit=40,
    )

    class Sandbox:
        def __init__(self, timeout):
            self.timeout = timeout
            self.closed = False
            self.files = {"instruction.md": "initial"}
            self.sandbox = SimpleNamespace(
                object_id=f"sandbox-{len(state.sandboxes)}",
                filesystem=SimpleNamespace(copy_from_local=SimpleNamespace(aio=self.copy)),
            )
            state.sandboxes.append(self)

        async def copy(self, path, destination):
            self.files[destination.removeprefix("/output/task/")] = path.read_text()

        async def start(self):
            state.events.append("start")

        async def prepare(self, source):
            state.events.append("prepare")

        async def stop(self):
            state.events.append("stop")
            if state.stop_error:
                raise state.stop_error
            self.closed = True
            if state.mutate_during_stop:
                (state.root / "revision-0/task/instruction.md").write_text("Changed after handoff")

        async def shell(self, *args, **kwargs):
            return '{"exit_code": 0}'

        async def write(self, path, content):
            self.files[path.removeprefix("/output/task/")] = content

        async def export(self, destination):
            if state.bad_export:
                state.bad_export = False
                raise ValueError("Invalid export fixture")
            destination.mkdir(parents=True)
            for name, content in self.files.items():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)

    async def agent(**kwargs):
        sandbox = state.sandboxes[-1]
        state.author_inputs.append(dict(sandbox.files))
        sandbox.files["instruction.md"] = f"authored-{len(state.author_inputs)}"
        kwargs["trace"].write_text('{"kind":"model"}\n')
        state.clock += state.active_seconds
        if state.callback:
            if state.bad_export:
                result = await kwargs["handlers"]["validate_candidate"]()
                assert "Structural validation failed" in result
            await kwargs["handlers"]["validate_candidate"]()
            if state.change_after_callback:
                sandbox.files["instruction.md"] += "-changed"

    def evidence(task, output, label, reward, error=None):
        path = output / (label + "-runtime")
        path.mkdir(parents=True, exist_ok=True)
        (path / "observation.txt").write_text("Retained mock execution")
        return TrialEvidence(
            label=label,
            reward=reward,
            error=error,
            path=str(path),
            task_digest=campaign.digest_task(task),
        )

    async def preflight(task, output, **kwargs):
        assert not state.sandboxes[-1].closed
        state.preflights.append(task)
        if state.invalid == "preflight":
            return [evidence(task, output, "baseline", None, "Provider unavailable")]
        return [evidence(task, output, "baseline", 0), evidence(task, output, "oracle-0", 1)]

    async def trial(task, output, label, **kwargs):
        state.events.append("trial:" + label)
        state.full_trials.append(
            (label, state.sandboxes[-1].closed, state.budget.spent, kwargs["budget"])
        )
        state.clock += state.validation_seconds
        if state.trial_ceiling is not None:
            reservation = state.budget.reserve(state.trial_ceiling, "trial admission fixture")
            state.budget.settle(reservation, 0)
        if state.model_budget_error and label.startswith("solver"):
            raise BudgetExceeded("No model request fits")
        if label == state.invalid:
            return evidence(task, output, label, None, "Provider unavailable")
        reward = int(label.startswith(("oracle", "equivalent")))
        if state.fail_control and label == "mutation-wrong" and len(state.author_inputs) == 1:
            reward = 1
        return evidence(task, output, label, reward)

    async def review(*args, **kwargs):
        state.events.append("review")
        return (
            {"status": "accepted", "reasons": [], "execution_errors": []},
            SimpleNamespace(adversary_assessment="attempted_hack"),
        )

    design = SimpleNamespace(
        verification_plan=SimpleNamespace(model_dump=lambda: {}, model_dump_json=lambda **k: "{}"),
        model_dump=lambda: {},
        model_dump_json=lambda: "{}",
    )

    async def plan(**kwargs):
        return design

    async def static_review(*args, **kwargs):
        return SimpleNamespace(passed=True)

    monkeypatch.setattr(campaign, "AuthorSandbox", Sandbox)
    monkeypatch.setattr(campaign, "run_agent", agent)
    monkeypatch.setattr(campaign, "preflight", preflight)
    monkeypatch.setattr(campaign, "trial", trial)
    # Lifecycle tests are orthogonal to the independently tested receipt validator.
    monkeypatch.setattr(campaign, "_review_revision", review)
    monkeypatch.setattr(campaign, "plan_candidate_design", plan)
    monkeypatch.setattr(campaign, "check_verification_plan", lambda *a: None)
    monkeypatch.setattr(campaign, "specification_snapshot", lambda *a: {})
    monkeypatch.setattr(campaign, "verifier_snapshot", lambda *a: {})
    monkeypatch.setattr(campaign, "review_specification", static_review)
    monkeypatch.setattr(campaign, "review_verifier", static_review)
    monkeypatch.setattr(
        campaign,
        "finalize",
        lambda *args: SimpleNamespace(
            mutations=[SimpleNamespace(name="wrong", script="false")],
            equivalents=[SimpleNamespace(name="other", script="true")],
            source_paths=["src/pkg"],
        ),
    )
    return state


def config(**kwargs):
    return CampaignConfig(
        max_revisions=2,
        max_candidate_drafts=4,
        author_timeout_sec=120,
        oracle_repeats=2,
        **kwargs,
    )


async def run(state, cfg=None):
    return await campaign.curate_one(
        state.source, state.root, cfg or config(release_author_before_validation=True), state.budget
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("release", [False, True])
async def test_release_is_opt_in_and_precedes_all_full_validation(scenario, release):
    state = scenario
    result = await run(state, config(release_author_before_validation=release))
    assert result["status"] == "accepted"
    assert len(state.sandboxes) == 1
    assert all(closed == release for _, closed, _, _ in state.full_trials)
    assert all(budget is state.budget for *_, budget in state.full_trials)
    assert state.events.count("stop") == 1
    if release:
        assert state.events.index("stop") < state.events.index("trial:oracle-1")
        assert state.full_trials[0][2] == pytest.approx(0.151)
        journal = json.loads((state.root / "author-phase.json").read_text())
        assert journal["active_seconds"] == 20
        assert journal["used_author_revisions"] == 1
        assert journal["sessions"][0]["status"] == "closed"
    else:
        assert state.events.index("review") < state.events.index("stop")
        assert state.full_trials[0][2] == 1.5
        assert not (state.root / "author-phase.json").exists()


@pytest.mark.asyncio
async def test_real_repair_restores_task_and_shares_timeout_and_submission_counters(scenario):
    state = scenario
    state.fail_control = state.callback = state.bad_export = True
    cfg = config(
        release_author_before_validation=True,
        submission_policy="conversion",
        require_verification_plan=True,
        specification_review=True,
        verifier_review=True,
    )
    result = await run(state, cfg)
    assert result["status"] == "accepted"
    assert [sandbox.timeout for sandbox in state.sandboxes] == [120, 100]
    assert state.author_inputs[1]["instruction.md"] == "authored-1"
    assert len(json.loads((state.root / "submitted-drafts.json").read_text())) == 2
    assert len(json.loads((state.root / "mechanical-submissions.json").read_text())) == 1
    journal = json.loads((state.root / "author-phase.json").read_text())
    assert journal["used_author_revisions"] == 2
    assert journal["active_seconds"] == 40
    assert len(journal["sessions"]) == 2
    assert all(session["status"] == "closed" for session in journal["sessions"])
    assert state.budget.spent == pytest.approx(0.302)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["preflight", "oracle-1", "solver-0-0", "adversary"])
async def test_incomplete_execution_never_starts_author_repair(scenario, failure):
    state = scenario
    state.invalid = failure
    with pytest.raises(campaign.ValidationExecutionError, match="did not complete"):
        await run(state)
    assert len(state.sandboxes) == len(state.author_inputs) == 1
    assert "review" not in state.events
    if failure != "preflight":
        assert state.full_trials[-1][0] == failure
        evidence = json.loads((state.root / "revision-0/evidence.json").read_text())
        assert evidence["trials"][-1]["error"] == "Provider unavailable"


@pytest.mark.asyncio
async def test_model_reservation_failure_never_reopens_author(scenario):
    state = scenario
    state.model_budget_error = True
    with pytest.raises(BudgetExceeded):
        await run(state)
    assert len(state.sandboxes) == 1
    assert state.sandboxes[0].closed
    assert "review" not in state.events


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("Termination unknown"), asyncio.CancelledError()])
async def test_uncertain_stop_retains_reservation_and_blocks_validation(scenario, failure):
    state = scenario
    state.stop_error = failure
    with pytest.raises(type(failure)):
        await run(state)
    assert state.events.count("stop") == 1
    assert not state.full_trials
    assert state.budget.spent == 1.5
    journal = json.loads((state.root / "author-phase.json").read_text())
    assert journal["status"] == "stop_uncertain"
    entries = json.loads(state.budget.path.read_text())["entries"]
    assert next(iter(entries.values()))["status"] == "reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_cached_preflight_is_reused_only_for_the_exact_final_digest(scenario, changed):
    state = scenario
    state.callback = True
    state.change_after_callback = changed
    await run(state)
    assert len(state.preflights) == (2 if changed else 1)
    evidence = json.loads((state.root / "revision-0/evidence.json").read_text())
    digest = campaign.digest_task(state.root / "revision-0/task")
    assert all(item["task_digest"] == digest for item in evidence["trials"])
    for item in evidence["trials"][:2]:
        assert Path(item["path"]).is_relative_to(state.root / "revision-0/trials")
        assert Path(item["path"]).is_dir()


@pytest.mark.asyncio
async def test_exhausted_active_time_does_not_get_a_fresh_timeout(scenario):
    state = scenario
    state.active_seconds = 120
    state.fail_control = True
    with pytest.raises(TimeoutError, match="Cumulative author"):
        await run(state)
    assert len(state.sandboxes) == 1
    assert len(state.author_inputs) == 1
    assert state.events.count("stop") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("release", [True, False])
@pytest.mark.parametrize("entry", ["curate_one", "_curate_one"])
async def test_existing_phase_cannot_restart_with_reset_counters_or_reservation(
    scenario, release, entry
):
    state = scenario
    await run(state)
    ledger = state.budget.path.read_bytes()
    journal = (state.root / "author-phase.json").read_bytes()
    source = (state.root / "source.json").read_bytes()
    before = {str(path): path.read_bytes() for path in state.root.rglob("*") if path.is_file()}
    calls = list(state.events)
    state.source = {"id": "changed", "url": "https://example.test/pull/other"}
    with pytest.raises(campaign.RecoveryError, match="explicit recovery"):
        await getattr(campaign, entry)(
            state.source, state.root, config(release_author_before_validation=release), state.budget
        )
    assert len(state.sandboxes) == 1
    assert state.budget.path.read_bytes() == ledger
    assert (state.root / "author-phase.json").read_bytes() == journal
    assert (state.root / "source.json").read_bytes() == source
    assert {
        str(path): path.read_bytes() for path in state.root.rglob("*") if path.is_file()
    } == before
    assert state.events == calls


@pytest.mark.asyncio
async def test_retained_phase_blocks_repair_claim_and_accounting_before_any_write(
    scenario, monkeypatch
):
    state = scenario
    await run(state)
    before = {str(path): path.read_bytes() for path in state.root.rglob("*") if path.is_file()}
    ledger = state.budget.path.read_bytes()
    calls = list(state.events)

    def forbidden_claim(*args, **kwargs):
        pytest.fail("Retained phase must be rejected before a repair claim")

    monkeypatch.setattr(campaign, "prepare_seed_repair", forbidden_claim)
    with pytest.raises(campaign.RecoveryError, match="explicit recovery"):
        await campaign.curate_one(
            {"id": "other", "url": "https://example.test/other"},
            state.root,
            config(release_author_before_validation=False),
            state.budget,
            state.root / "revision-0/task",
            seed_repair=object(),
        )
    assert {
        str(path): path.read_bytes() for path in state.root.rglob("*") if path.is_file()
    } == before
    assert state.budget.path.read_bytes() == ledger
    assert state.events == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("release", [False, True])
async def test_early_settlement_allows_request_within_original_candidate_cap(scenario, release):
    state = scenario
    state.trial_ceiling = 7.7
    cfg = config(release_author_before_validation=release)
    if release:
        assert (await run(state, cfg))["status"] == "accepted"
    else:
        with pytest.raises(BudgetExceeded, match="Candidate budget"):
            await run(state, cfg)
    assert len(state.sandboxes) == 1
    assert state.budget.scope_limit == 8


@pytest.mark.asyncio
async def test_reopens_share_the_original_author_cloud_allowance(scenario):
    state = scenario
    state.root.mkdir()
    phase = campaign._AuthorPhases(
        state.root, state.source, config(author_cloud_allowance_usd=1), state.budget, 0
    )
    for _ in range(6):
        await phase.start()
        assert state.budget.spent == pytest.approx(1)
        await phase.close()
    with pytest.raises(BudgetExceeded, match="Cumulative author cloud"):
        await phase.start()
    assert len(state.sandboxes) == 6
    assert state.budget.spent == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_final_digest_is_rechecked_after_sandbox_shutdown(scenario):
    state = scenario
    state.mutate_during_stop = True
    with pytest.raises(campaign.RecoveryError, match="changed while closing"):
        await run(state)
    assert not state.full_trials
    assert state.budget.spent == pytest.approx(0.151)


@pytest.mark.asyncio
async def test_assisted_repair_keeps_inherited_revision_and_submission_history(scenario, tmp_path):
    state = scenario
    family = retained_repair_family.__wrapped__(tmp_path)
    cfg = family.config.model_copy(
        update={"release_author_before_validation": True, "author_timeout_sec": 120}
    )
    context = family.context.model_copy(
        update={"config": write(family.context.config.path, cfg.model_dump())}
    )
    state.root, state.source, state.budget = family.root, family.source, family.budget
    state.fail_control = True
    result = await campaign.curate_one(
        state.source,
        state.root,
        cfg,
        state.budget,
        family.task,
        seed_repair=context,
    )
    assert result["status"] == "accepted"
    assert result["kind"] == "assisted_autonomous_repair"
    assert len(state.sandboxes) == 2
    assert [sandbox.timeout for sandbox in state.sandboxes] == [120, 100]
    assert len(json.loads((state.root / "submitted-drafts.json").read_text())) == 4
    assert len(json.loads((state.root / "mechanical-submissions.json").read_text())) == 1
    assert (
        json.loads((state.root / "repair-progress.json").read_text())["used_author_revisions"] == 3
    )
    assert json.loads((state.root / "author-phase.json").read_text())["used_author_revisions"] == 3
    assert json.loads((state.root / "repair-accounting.json").read_text())["lineage_scopes"] == [
        "parent-scope",
        "repair-scope",
    ]
