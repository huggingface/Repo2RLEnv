from __future__ import annotations

import importlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from repo2rlenv.curation import artifacts, campaign
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.inference import inference_digest
from repo2rlenv.curation.models import (
    CRITERIA,
    CampaignConfig,
    Review,
    TrialEvidence,
    execution_gate_reasons,
)

review_module = importlib.import_module("repo2rlenv.curation.review")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def checkpoint(path: Path, modified: int = 100) -> Path:
    write(path / "contract.json", "{}")
    os.utime(path / "contract.json", ns=(modified, modified))
    return path


@pytest.mark.parametrize("older_modified", [100, 999])
def test_checkpoint_chooses_newest_attempt_before_preserved_file_mtimes(tmp_path, older_modified):
    older = checkpoint(tmp_path / "9/revision-0/task")
    newer = tmp_path / "10/revision-0/task"
    shutil.copytree(older, newer)
    os.utime(older / "contract.json", ns=(older_modified, older_modified))
    assert campaign.latest_checkpoint(tmp_path) == newer


def test_checkpoint_ignores_review_archives_and_nested_snapshots(tmp_path):
    expected = checkpoint(tmp_path / "10/revision-1/task")
    for path in (
        "10/prior-review/task",
        "10/prior-review/revision-99/task",
        "10/snapshots/revision-99/task",
        "10/drafts/2/snapshot/task",
        "11/prior-review/revision-99/task",
        "11/revision-1/nested/task",
    ):
        checkpoint(tmp_path / path, modified=999)
    assert campaign.latest_checkpoint(tmp_path) == expected


@pytest.mark.parametrize("latest", ["drafts/2", "revision-1"])
def test_checkpoint_uses_latest_task_edits_not_later_review_evidence(tmp_path, latest):
    draft = checkpoint(tmp_path / "10/drafts/2/task")
    revision = checkpoint(tmp_path / "10/revision-1/task")
    expected = tmp_path / "10" / latest / "task"
    write(expected / "solution/solve.sh", "echo latest")
    os.utime(expected / "solution/solve.sh", ns=(200, 200))
    other = revision if expected == draft else draft
    write(other.parent / "review.json", "{}")
    os.utime(other.parent / "review.json", ns=(999, 999))
    os.utime(other.parent, ns=(999, 999))
    assert campaign.latest_checkpoint(tmp_path) == expected


def test_checkpoint_ties_prefer_revision_then_numeric_ordinal(tmp_path):
    checkpoint(tmp_path / "10/drafts/100/task")
    checkpoint(tmp_path / "10/revision-2/task")
    expected = checkpoint(tmp_path / "10/revision-10/task")
    assert campaign.latest_checkpoint(tmp_path) == expected
    shutil.rmtree(tmp_path / "10/revision-10")
    shutil.rmtree(tmp_path / "10/revision-2")
    checkpoint(tmp_path / "10/drafts/9/task")
    assert campaign.latest_checkpoint(tmp_path) == tmp_path / "10/drafts/100/task"


@pytest.mark.parametrize("incomplete", ["empty", "no_contract"])
def test_checkpoint_falls_back_when_newest_attempt_has_no_task(tmp_path, incomplete):
    expected = checkpoint(tmp_path / "10/revision-0/task")
    (tmp_path / "11").mkdir()
    if incomplete == "no_contract":
        write(tmp_path / "11/revision-0/task/instruction.md", "unfinished export")
    assert campaign.latest_checkpoint(tmp_path) == expected
    shutil.rmtree(tmp_path / "10")
    assert campaign.latest_checkpoint(tmp_path) is None


@pytest.mark.parametrize("linked", ["attempt", "drafts", "checkpoint", "task", "contract", "file"])
def test_checkpoint_rejects_symlinked_paths_and_contents(tmp_path, linked):
    parent = tmp_path / "candidates"
    expected = checkpoint(parent / "10/revision-0/task")
    task = checkpoint(parent / "11/drafts/1/task", modified=999)
    path = {
        "attempt": parent / "11",
        "drafts": parent / "11/drafts",
        "checkpoint": task.parent,
        "task": task,
        "contract": task / "contract.json",
        "file": task / "instruction.md",
    }[linked]
    if linked == "file":
        write(path, "symlinked task content")
    outside = tmp_path / "outside"
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    assert campaign.latest_checkpoint(parent) == expected
    alias = tmp_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    assert campaign.latest_checkpoint(alias) is None


@pytest.mark.asyncio
async def test_campaign_passes_newest_attempt_checkpoint_to_author(tmp_path, monkeypatch):
    source = {"id": "example-project-1", "url": "https://github.com/example/project/pull/1"}
    parent = tmp_path / "candidates" / source["id"]
    older = checkpoint(parent / "9/revision-0/task")
    newer = parent / "10/revision-0/task"
    shutil.copytree(older, newer)
    checkpoint(parent / "10/prior-review/task", modified=999)
    (parent / "11").mkdir()
    author = AsyncMock(return_value={"source": source["url"], "status": "rejected", "reasons": []})
    monkeypatch.setattr(campaign, "curate_one", author)
    monkeypatch.setattr(campaign, "resolve_pr", lambda _: source)
    monkeypatch.setattr(campaign, "version", lambda _: "test")
    config = CampaignConfig(target=1)
    campaign.save(tmp_path / "config.json", config.model_dump())
    campaign.save(tmp_path / "budget.json", {"entries": {}})
    await campaign.campaign([source["url"]], tmp_path, config)
    assert author.await_args.args[-1] == newer


def good_review() -> Review:
    return Review.model_validate(
        {
            "criteria": {
                name: {
                    "score": 4,
                    "outcome": "pass",
                    "explanation": "The retained execution evidence supports this criterion.",
                    "evidence": ["task/instruction.md"],
                }
                for name in CRITERIA
            },
            "blockers": [],
            "failure_attribution": {"solver-0-0": "reasoning", "solver-1-0": "solved"},
            "reward_hacks": [],
            "suggested_repairs": [],
            "adversary_assessment": "attempted_hack",
        }
    )


@pytest.fixture
def retained(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    source = {
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
        "id": "widget-pr",
        "url": "https://github.com/example/widgets/pull/1",
    }
    config = CampaignConfig(max_revisions=1)
    previous = tmp_path / "previous" / "revision-0"
    task = previous / "task"
    content = {
        "instruction.md": "Fix empty and nested widget inputs in src/widget.",
        "environment/Dockerfile": "FROM python:3.12-slim@sha256:"
        + "a" * 64
        + "\nWORKDIR /workspace\nRUN curl "
        + source["base_sha"]
        + "\nRUN pip install pytest==8.4.2\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_contract.py": "from probe import run_probe\n"
        "def test_empty(): run_probe('print(0)')\n"
        "def test_nested(): pass\ndef test_general(): pass\n",
        "contract.json": json.dumps(
            {
                "title": "Widget inputs",
                "rationale": "A retained task for a pending review.",
                "source_paths": ["src/widget"],
                "requirements": [
                    {"id": "empty", "behavior": "empty inputs", "tests": ["test_empty"]},
                    {"id": "nested", "behavior": "nested inputs", "tests": ["test_nested"]},
                ],
                "mutations": [
                    {"name": "empty", "rationale": "miss empty inputs", "script": "true"},
                    {"name": "nested", "rationale": "miss nested inputs", "script": "true"},
                ],
                "equivalents": [
                    {"name": "alternative", "rationale": "valid alternative", "script": "true"}
                ],
                "min_tests": 3,
            }
        ),
    }
    for name, text in content.items():
        write(task / name, text)
    artifacts.finalize(task, source)
    digest = artifacts.digest_task(task)
    rewards = {
        "baseline": 0,
        "oracle-0": 1,
        "oracle-1": 1,
        "oracle-2": 1,
        "tamper": 0,
        "pytest-tamper": 0,
        "mutation-empty": 0,
        "mutation-nested": 0,
        "equivalent-alternative": 1,
        "solver-0-0": 0,
        "solver-1-0": 1,
        "adversary": 0,
    }
    trials = []
    for label, reward in rewards.items():
        # The real Harbor trial directory includes an ID, separate from its label.
        folder = previous / "trials" / (label + "-retained-id")
        model = None
        if label.startswith("solver-"):
            model = config.solver_models[int(label.split("-")[1])]
        elif label == "adversary":
            model = config.author_model
        evidence = TrialEvidence(
            label=label,
            reward=reward,
            task_digest=digest,
            path=str(folder),
            model=model,
            inference_digest=inference_digest(model, adversary=label == "adversary")
            if model
            else None,
        )
        write(
            folder / "result.json", json.dumps({"verifier_result": {"rewards": {"reward": reward}}})
        )
        write(folder / "agent/trajectory.jsonl", json.dumps({"label": label, "model": model}))
        write(folder / "verifier/reward.txt", str(reward))
        write(
            folder / "artifacts/manifest.json",
            json.dumps(
                [
                    {
                        "source": "/workspace/src/widget",
                        "destination": "artifacts/workspace/src/widget",
                        "status": "ok",
                    }
                ]
            ),
        )
        write(folder / "artifacts/workspace/src/widget/widget.py", f"value = {reward}\n")
        campaign.save(previous / "trials" / f"{label}.json", evidence.model_dump())
        trials.append(evidence)
    metadata = {
        "admission_version": campaign.ADMISSION_VERSION,
        "task_digest": digest,
        "trials": [t.model_dump() for t in trials],
    }
    campaign.save(previous / "evidence.json", metadata)
    write(previous / "judge-state.json", '{"messages": [{"content": "old judge fragment"}]}')
    write(
        previous / "judge-trace.jsonl",
        '{"kind":"review_finalization","phase":"review_finalization"}\n',
    )
    write(previous / "review-evidence.json", '{"old": "judge evidence index"}')

    # Every paid author/rollout route fails immediately in these tests.
    sandbox = Mock(side_effect=AssertionError("Unexpected author sandbox"))
    author = AsyncMock(side_effect=AssertionError("Unexpected author model"))
    preflight = AsyncMock(side_effect=AssertionError("Unexpected preflight"))
    trial = AsyncMock(side_effect=AssertionError("Unexpected trial"))
    monkeypatch.setattr(campaign, "AuthorSandbox", sandbox)
    monkeypatch.setattr(campaign, "run_agent", author)
    monkeypatch.setattr(campaign, "preflight", preflight)
    monkeypatch.setattr(campaign, "trial", trial)
    budget = Budget(tmp_path / "budget.json", 20)
    reserve = Mock(side_effect=AssertionError("Unexpected model or cloud reservation"))
    monkeypatch.setattr(budget, "reserve", reserve)
    judge = AsyncMock(return_value={"messages": [{"content": good_review().model_dump_json()}]})
    monkeypatch.setattr(review_module, "run_agent", judge)
    monkeypatch.setattr(
        review_module, "completion", AsyncMock(side_effect=AssertionError("Unexpected paid call"))
    )
    return SimpleNamespace(
        source=source,
        config=config,
        previous=previous,
        task=task,
        root=tmp_path / "resumed",
        digest=digest,
        metadata=metadata,
        budget=budget,
        sandbox=sandbox,
        author=author,
        preflight=preflight,
        trial=trial,
        reserve=reserve,
        judge=judge,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_review", [None, '{"criteria":', '{"invalid": "schema"}'])
@pytest.mark.parametrize("legacy_version", [False, True])
async def test_pending_review_reuses_actual_evidence_without_author_or_rollouts(
    retained, prior_review, legacy_version
):
    r = retained
    if prior_review is not None:
        write(r.previous / "review.json", prior_review)
    if legacy_version:
        del r.metadata["admission_version"]
        campaign.save(r.previous / "evidence.json", r.metadata)
    before = artifacts.digest_task(r.previous)

    async def judge(**kwargs):
        assert kwargs["model"] == r.config.judge_model
        assert kwargs["budget"] is r.budget
        assert kwargs["trace"].parent == r.root / "revision-0"
        assert not kwargs["trace"].exists()
        assert "old judge fragment" not in kwargs["prompt"]
        assert "prior-review/" not in kwargs["prompt"]
        read = kwargs["handlers"]["read_evidence"]
        instruction = await read("task/instruction.md")
        assert instruction.partition("\n")[2] == (r.task / "instruction.md").read_text()
        path = "trials/solver-1-0-retained-id/agent/trajectory.jsonl"
        assert (await read(path)).partition("\n")[2] == (r.previous / path).read_text()
        changed = "trials/solver-1-0-retained-id/artifacts/workspace/src/widget/widget.py"
        assert (await read(changed)).partition("\n")[2] == (r.previous / changed).read_text()
        return {"messages": [{"content": good_review().model_dump_json()}]}

    r.judge.side_effect = judge
    result = await campaign.curate_one(r.source, r.root, r.config, r.budget, r.task)
    assert result["status"] == "accepted"
    assert result["score"] == 100
    assert result["task_digest"] == r.digest
    assert result["resumed_from"] == str(r.previous)
    assert result["admission_version"] == campaign.ADMISSION_VERSION
    assert result["human_review"] == "pending"
    assert json.loads((r.root / "verdict.json").read_text()) == result
    assert Review.model_validate_json(Path(result["review_path"]).read_text()) == good_review()
    assert artifacts.digest_task(Path(result["task_path"])) == r.digest
    assert artifacts.digest_task(r.previous) == before
    assert (r.root / "prior-review/judge-state.json").read_bytes() == (
        r.previous / "judge-state.json"
    ).read_bytes()
    copied = json.loads((r.root / "revision-0/evidence.json").read_text())
    assert copied["admission_version"] == campaign.ADMISSION_VERSION
    assert copied["resumed_from"] == str(r.previous)
    original_trials = {t["label"]: t for t in r.metadata["trials"]}
    for new in copied["trials"]:
        old = original_trials[new["label"]]
        old_path, new_path = Path(old["path"]), Path(new["path"])
        assert new_path == r.root / "revision-0" / old_path.relative_to(r.previous)
        assert artifacts.digest_task(new_path) == artifacts.digest_task(old_path)
        sidecar = r.root / "revision-0/trials" / f"{new['label']}.json"
        assert json.loads(sidecar.read_text()) == new
    r.judge.assert_awaited_once()
    for forbidden in (r.sandbox, r.author, r.preflight, r.trial, r.reserve):
        forbidden.assert_not_called()
    assert r.budget.spent == 0


class AuthorRepairRequired(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_recovered_quality_rejection_enters_author_repair(retained):
    r = retained
    rejected = good_review()
    rejected.blockers = ["The instruction needs a concrete expected behavior."]
    r.judge.return_value = {"messages": [{"content": rejected.model_dump_json()}]}
    r.sandbox.side_effect = AuthorRepairRequired("ordinary author repair")
    with pytest.raises(AuthorRepairRequired, match="ordinary author repair"):
        await campaign.curate_one(r.source, r.root, r.config, r.budget, r.task)
    r.judge.assert_awaited_once()
    r.sandbox.assert_called_once()
    r.trial.assert_not_called()
    assert Review.model_validate_json((r.root / "revision-0/review.json").read_text()) == rejected


async def assert_author_fallback(r) -> None:
    before = artifacts.digest_task(r.task)
    r.sandbox.side_effect = AuthorRepairRequired("ordinary author repair")
    with pytest.raises(AuthorRepairRequired, match="ordinary author repair"):
        await campaign.curate_one(r.source, r.root, r.config, r.budget, r.task)
    r.sandbox.assert_called_once_with(r.config.author_timeout_sec)
    r.judge.assert_not_called()
    r.reserve.assert_not_called()
    assert artifacts.digest_task(r.task) == before
    assert not (r.root / "revision-0").exists()
    assert not (r.root / "prior-review").exists()
    assert not list(r.root.glob(".pending-review-*"))


def allow_reruns(r) -> None:
    async def rerun(task, output, label, *, config, budget, **kwargs):
        assert config is r.config and budget is r.budget
        assert task == r.root / "revision-0/task"
        folder = output / (label + "-fresh-id")
        old = r.previous / "trials" / (label + "-retained-id")
        if old.is_dir():
            shutil.copytree(old, folder)
        else:
            write(folder / "result.json", "{}")
        model = kwargs.get("model")
        reward = int(label.startswith(("oracle-", "equivalent-", "solver-1-")))
        result = TrialEvidence(
            label=label,
            task_digest=r.digest,
            path=str(folder),
            model=model,
            inference_digest=inference_digest(model, adversary=label == "adversary")
            if model
            else None,
            reward=reward,
        )
        campaign.save(output / f"{label}.json", result.model_dump())
        return result

    r.trial.side_effect = rerun
    result = good_review()
    result.failure_attribution = {
        f"solver-{i}-{k}": "solved" if i == 1 else "reasoning"
        for i in range(len(r.config.solver_models))
        for k in range(r.config.solver_attempts)
    }
    r.judge.return_value = {"messages": [{"content": result.model_dump_json()}]}


async def assert_validation_resume(r, rerun_labels) -> dict:
    allow_reruns(r)
    result = await campaign.curate_one(r.source, r.root, r.config, r.budget, r.task)
    assert result["status"] == "accepted"
    assert [call.args[2] for call in r.trial.await_args_list] == rerun_labels
    assert result["recovery"]["rerun_trials"] == rerun_labels
    assert set(result["recovery"]["reused_trials"]).isdisjoint(rerun_labels)
    assert result["admission_version"] == campaign.ADMISSION_VERSION
    r.sandbox.assert_not_called()
    r.author.assert_not_called()
    r.preflight.assert_not_called()
    r.reserve.assert_not_called()
    r.judge.assert_awaited_once()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["task", "wrapper", "version", "trial_digest"])
async def test_stale_task_wrapper_or_admission_evidence_falls_back(retained, monkeypatch, change):
    r = retained
    if change == "task":
        write(r.task / "instruction.md", "Changed task after trials.")
    elif change == "wrapper":
        monkeypatch.setattr(artifacts, "VERIFIER", artifacts.VERIFIER + "\n# new wrapper\n")
    elif change == "version":
        r.metadata["admission_version"] = 2
    else:
        r.metadata["trials"][0]["task_digest"] = "obsolete"
    campaign.save(r.previous / "evidence.json", r.metadata)
    await assert_author_fallback(r)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label",
    [
        "baseline",
        "oracle-0",
        "oracle-1",
        "oracle-2",
        "tamper",
        "pytest-tamper",
        "mutation-empty",
        "mutation-nested",
        "equivalent-alternative",
        "solver-0-0",
        "solver-1-0",
        "adversary",
    ],
)
async def test_only_missing_execution_gate_is_run_without_author(retained, label):
    r = retained
    r.metadata["trials"] = [t for t in r.metadata["trials"] if t["label"] != label]
    (r.previous / "trials" / f"{label}.json").unlink()
    campaign.save(r.previous / "evidence.json", r.metadata)
    await assert_validation_resume(r, [label])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "field", "value"),
    [
        ("baseline", "reward", 1),
        ("oracle-2", "reward", 0),
        ("tamper", "reward", 1),
        ("pytest-tamper", "reward", 1),
        ("mutation-empty", "reward", 1),
        ("equivalent-alternative", "reward", 0),
    ],
)
async def test_failed_behavior_gate_requires_author_repair(retained, label, field, value):
    r = retained
    next(t for t in r.metadata["trials"] if t["label"] == label)[field] = value
    campaign.save(r.previous / "evidence.json", r.metadata)
    await assert_author_fallback(r)


@pytest.mark.asyncio
async def test_positive_adversary_reward_is_reviewed_before_attribution(retained):
    r = retained
    next(t for t in r.metadata["trials"] if t["label"] == "adversary")["reward"] = 1
    campaign.save(r.previous / "evidence.json", r.metadata)
    result = good_review()
    result.adversary_assessment = "solved_task"
    r.judge.return_value = {"messages": [{"content": result.model_dump_json()}]}
    verdict = await campaign.curate_one(r.source, r.root, r.config, r.budget, r.task)
    assert verdict["status"] == "execution_failure"
    assert not result.reward_hacks
    assert verdict["execution_errors"][0]["error"] == ("Incomplete adversarial audit: solved_task")
    r.judge.assert_awaited_once()
    r.sandbox.assert_not_called()
    r.trial.assert_not_called()


@pytest.mark.asyncio
async def test_incomplete_audit_retries_only_audit_when_quality_is_valid(retained):
    r = retained
    review = good_review()
    review.adversary_assessment = "solved_task"
    write(r.previous / "review.json", review.model_dump_json())
    for trial in r.metadata["trials"]:
        if trial["label"] == "adversary":
            trial["error"] = "Incomplete adversarial audit: solved_task"
            trial["reward"] = 1
    campaign.save(r.previous / "evidence.json", r.metadata)
    await assert_validation_resume(r, ["adversary"])


@pytest.mark.asyncio
async def test_incomplete_audit_with_quality_defect_requires_author_repair(retained):
    r = retained
    review = good_review()
    review.adversary_assessment = "solved_task"
    review.blockers = ["The instruction reveals the reference implementation algorithm."]
    write(r.previous / "review.json", review.model_dump_json())
    await assert_author_fallback(r)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["oracle_repeats", "solver_attempts", "solver_models", "author_model"]
)
async def test_only_missing_evidence_for_current_config_is_run(retained, change):
    r = retained
    updated = {
        "oracle_repeats": 4,
        "solver_attempts": 2,
        "solver_models": [r.config.solver_models[0], "changed-opus-model"],
        "author_model": "changed-adversary-model",
    }
    r.config = r.config.model_copy(update={change: updated[change]})
    expected = {
        "oracle_repeats": ["oracle-3"],
        "solver_attempts": ["solver-0-1", "solver-1-1"],
        "solver_models": ["solver-1-0"],
        "author_model": ["adversary"],
    }
    await assert_validation_resume(r, expected[change])


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_valid_previous_quality_verdict_is_never_rejudged(retained, accepted):
    r = retained
    result = good_review()
    if not accepted:
        result.blockers = ["The verifier misses required behavior."]
    write(r.previous / "review.json", result.model_dump_json())
    await assert_author_fallback(r)


@pytest.mark.asyncio
async def test_resumed_quality_rejection_is_persisted_before_author_repair(retained):
    r = retained
    rejected = good_review()
    rejected.blockers = ["The verifier misses required behavior."]
    r.judge.return_value = {"messages": [{"content": rejected.model_dump_json()}]}
    r.root.mkdir(parents=True)
    pending = campaign._prepare_pending_review(r.task, r.root, r.source, r.config)
    assert pending is not None
    result = await campaign._resume_validation(
        r.source, r.root, r.config, r.budget, r.task, pending
    )
    assert result["status"] == "rejected"
    assert result["reasons"] == rejected.blockers
    assert result["admission_version"] == campaign.ADMISSION_VERSION
    assert result["resumed_from"] == str(r.previous)
    assert json.loads((r.root / "verdict.json").read_text()) == result
    r.sandbox.assert_not_called()
    r.trial.assert_not_called()
    r.judge.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_failure_preserves_new_checkpoint_and_never_falls_back_to_paid_trials(
    retained,
):
    r = retained
    r.judge.side_effect = ValueError("Review still incomplete")
    with pytest.raises(ValueError, match="Review still incomplete"):
        await campaign.curate_one(r.source, r.root, r.config, r.budget, r.task)
    assert (r.root / "revision-0/evidence.json").is_file()
    assert not (r.root / "verdict.json").exists()
    r.sandbox.assert_not_called()
    r.trial.assert_not_called()
    r.reserve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{", "[]", '{"trials": []}'])
async def test_missing_or_invalid_evidence_falls_back(retained, content):
    r = retained
    write(r.previous / "evidence.json", content)
    await assert_author_fallback(r)


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["outside_trial", "linked_artifact", "special_file"])
async def test_unsafe_or_missing_retained_files_are_not_reused(retained, unsafe):
    r = retained
    if unsafe == "outside_trial":
        outside = r.previous.parent / "outside"
        write(outside / "result.json", "{}")
        r.metadata["trials"][0]["path"] = str(outside)
    elif unsafe == "linked_artifact":
        outside = r.previous.parent / "secret.txt"
        outside.write_text("Must not be followed by evidence copy")
        path = Path(r.metadata["trials"][0]["path"]) / "artifacts/link"
        path.symlink_to(outside)
    else:
        os.mkfifo(r.previous / "trials/special")
    campaign.save(r.previous / "evidence.json", r.metadata)
    await assert_author_fallback(r)


@pytest.mark.asyncio
async def test_fresh_revision_writes_admission_version(retained):
    r = retained
    r.root.mkdir()
    # Exercise the same review checkpoint writer used after ordinary authoring.
    folder = r.root / "revision-0"
    shutil.copytree(r.previous, folder)
    (folder / "judge-trace.jsonl").unlink()
    trials = [TrialEvidence.model_validate(t) for t in r.metadata["trials"]]
    for t in trials:
        t.path = str(folder / Path(t.path).relative_to(r.previous))
    contract = artifacts.finalize(folder / "task", r.source)
    verdict, _ = await campaign._review_revision(
        r.source, folder, r.config, r.budget, contract, r.digest, trials
    )
    metadata = json.loads((folder / "evidence.json").read_text())
    assert metadata["admission_version"] == campaign.ADMISSION_VERSION
    assert "resumed_from" not in metadata
    assert "resumed_from" not in verdict


@pytest.mark.asyncio
async def test_sidecars_recover_complete_validation_without_top_level_evidence(retained):
    r = retained
    (r.previous / "evidence.json").unlink()
    await assert_validation_resume(r, [])


@pytest.mark.asyncio
async def test_partial_validation_reuses_draft_preflight_and_controls_but_reruns_legacy_models(
    retained,
):
    r = retained
    (r.previous / "evidence.json").unlink()
    for label in ("baseline", "oracle-0"):
        sidecar = r.previous / "trials" / f"{label}.json"
        saved = TrialEvidence.model_validate_json(sidecar.read_text())
        old_path = Path(saved.path)
        draft_path = r.previous.parent / "drafts/2/trials" / old_path.name
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(draft_path)
        saved.path = str(draft_path)
        campaign.save(draft_path.parent / sidecar.name, saved.model_dump())
        sidecar.unlink()
    for label in ("solver-0-0", "solver-1-0", "adversary"):
        sidecar = r.previous / "trials" / f"{label}.json"
        data = json.loads(sidecar.read_text())
        del data["inference_digest"]
        campaign.save(sidecar, data)
    (r.previous / "trials/oracle-2.json").unlink()
    before = artifacts.digest_task(r.previous.parent)
    result = await assert_validation_resume(
        r, ["oracle-2", "solver-0-0", "solver-1-0", "adversary"]
    )
    assert artifacts.digest_task(r.previous.parent) == before
    sources = result["recovery"]["reused_trial_sources"]
    assert "/drafts/2/trials/" in sources["baseline"]
    assert "/drafts/2/trials/" in sources["oracle-0"]
    assert len(result["recovery"]["reused_trials"]) == 8
    assert len(result["recovery"]["discarded_trials"]) == 3
    assert "inference_digests" in result["recovery"]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, 3, 4])
@pytest.mark.parametrize("old_digest", [None, "obsolete-inference-settings"])
async def test_models_rerun_for_old_inference_policy_without_repeating_controls(
    retained, version, old_digest
):
    r = retained
    if version is None:
        del r.metadata["admission_version"]
    else:
        r.metadata["admission_version"] = version
    for t in r.metadata["trials"]:
        if t["model"]:
            t["inference_digest"] = old_digest
    campaign.save(r.previous / "evidence.json", r.metadata)
    await assert_validation_resume(r, ["solver-0-0", "solver-1-0", "adversary"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "field", "value"),
    [
        ("baseline", "error", "Cloud infrastructure unavailable"),
        ("solver-0-0", "error", "Provider interrupted"),
        ("solver-1-0", "model", "obsolete-model"),
        ("adversary", "model", "obsolete-model"),
        ("baseline", "path", "missing"),
    ],
)
async def test_errored_missing_or_wrong_model_trial_is_replaced(retained, label, field, value):
    r = retained
    if field == "path":
        value = str(r.previous / "trials/missing")
    next(t for t in r.metadata["trials"] if t["label"] == label)[field] = value
    campaign.save(r.previous / "evidence.json", r.metadata)
    await assert_validation_resume(r, [label])


@pytest.mark.asyncio
async def test_retained_trace_is_inspected_for_empty_model_output_before_reuse(retained):
    r = retained
    saved = next(t for t in r.metadata["trials"] if t["label"] == "solver-0-0")
    write(
        Path(saved["path"]) / "agent/trace.jsonl",
        json.dumps({"kind": "model", "message": {"content": None, "tool_calls": []}}) + "\n",
    )
    result = await assert_validation_resume(r, ["solver-0-0"])
    assert "no final text" in result["recovery"]["discarded_trials"][0]["reason"]


@pytest.mark.asyncio
async def test_duplicate_trial_labels_are_not_silently_merged(retained):
    r = retained
    r.metadata["trials"].append(r.metadata["trials"][0])
    campaign.save(r.previous / "evidence.json", r.metadata)
    await assert_author_fallback(r)


@pytest.mark.asyncio
async def test_new_trial_error_is_checkpointed_without_author_or_judge(retained):
    r = retained
    label = "solver-0-0"
    r.metadata["trials"] = [t for t in r.metadata["trials"] if t["label"] != label]
    (r.previous / "trials" / f"{label}.json").unlink()
    campaign.save(r.previous / "evidence.json", r.metadata)
    allow_reruns(r)
    successful_rerun = r.trial.side_effect

    async def errored(*args, **kwargs):
        result = await successful_rerun(*args, **kwargs)
        result.error = "Current provider still unavailable"
        return result

    r.trial.side_effect = errored
    verdict = await campaign.curate_one(r.source, r.root, r.config, r.budget, r.task)
    assert verdict["status"] == "execution_failure"
    assert verdict["execution_errors"][0]["error"] == "Current provider still unavailable"
    assert json.loads((r.root / "verdict.json").read_text()) == verdict
    r.trial.assert_awaited_once()
    r.sandbox.assert_not_called()
    r.judge.assert_not_called()


@pytest.mark.asyncio
async def test_new_behavior_failure_transfers_to_author_once_and_preserves_validation(retained):
    r = retained
    label = "mutation-empty"
    r.metadata["trials"] = [t for t in r.metadata["trials"] if t["label"] != label]
    (r.previous / "trials" / f"{label}.json").unlink()
    campaign.save(r.previous / "evidence.json", r.metadata)
    allow_reruns(r)
    successful_rerun = r.trial.side_effect

    async def failed_behavior(*args, **kwargs):
        result = await successful_rerun(*args, **kwargs)
        result.reward = 1
        return result

    r.trial.side_effect = failed_behavior
    r.sandbox.side_effect = AuthorRepairRequired("repair failed control")
    with pytest.raises(AuthorRepairRequired, match="repair failed control"):
        await campaign.curate_one(r.source, r.root, r.config, r.budget, r.task)
    metadata = json.loads((r.root / "revision-0/evidence.json").read_text())
    assert next(t for t in metadata["trials"] if t["label"] == label)["reward"] == 1
    r.trial.assert_awaited_once()
    r.sandbox.assert_called_once()
    r.judge.assert_not_called()


@pytest.mark.parametrize("label", ["solver-0-0", "solver-1-0", "adversary"])
def test_admission_rejects_old_inference_policy(retained, label):
    r = retained
    trials = [TrialEvidence.model_validate(t) for t in r.metadata["trials"]]
    assert (
        execution_gate_reasons(trials, r.config, r.digest, ["empty", "nested"], ["alternative"])
        == []
    )
    next(t for t in trials if t.label == label).inference_digest = None
    assert execution_gate_reasons(trials, r.config, r.digest, ["empty", "nested"], ["alternative"])
