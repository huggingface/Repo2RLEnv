from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.models import (
    CRITERIA,
    CampaignConfig,
    Contract,
    Review,
    TrialEvidence,
    acceptance,
)
from repo2rlenv.curation.sources import parse_seeds, resolve_pr


def test_budget_reservations_survive_restart_and_failed_calls(tmp_path):
    path = tmp_path / "ledger.json"
    budget = Budget(path, 1)
    key = budget.reserve(0.7, "request")
    restarted = Budget(path, 1)
    with pytest.raises(BudgetExceeded):
        restarted.reserve(0.4, "another")
    restarted.settle(key, 0.2)
    assert budget.spent == pytest.approx(0.2)
    with pytest.raises(ValueError, match="already settled"):
        budget.settle(key, 0)


def test_concurrent_budget_cannot_overspend(tmp_path):
    path = tmp_path / "ledger.json"

    def attempt(_):
        try:
            Budget(path, 1).reserve(0.3, "concurrent")
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(attempt, range(20))) == 3
    assert Budget(path, 1).spent == pytest.approx(0.9)


def test_candidate_budgets_share_global_cap_without_cross_charging(tmp_path):
    path = tmp_path / "ledger.json"
    a = Budget(path, 1, scope="a", scope_limit=0.7)
    b = Budget(path, 1, scope="b", scope_limit=0.7)
    a.reserve(0.6, "a")
    b.reserve(0.3, "b")
    assert a.spent == pytest.approx(0.6)
    assert b.spent == pytest.approx(0.3)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.2, "global limit")
    with pytest.raises(BudgetExceeded):
        a.reserve(0.15, "candidate limit")
    assert Budget(path, 1).spent == pytest.approx(0.9)


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1, 0])
def test_invalid_budget_values(tmp_path, amount):
    with pytest.raises(ValueError):
        Budget(tmp_path / "ledger.json", amount)
    with pytest.raises(ValueError):
        Budget(tmp_path / "ledger.json", 1).reserve(amount, "bad")


@pytest.fixture
def good_review():
    return Review.model_validate(
        {
            "criteria": {
                k: {
                    "score": 4,
                    "outcome": "pass",
                    "explanation": "Observed complete behavioral evidence.",
                    "evidence": ["tests/test_contract.py::test_behavior"],
                }
                for k in CRITERIA
            },
            "blockers": [],
            "failure_attribution": {"solver-0-0": "reasoning", "solver-1-0": "solved"},
            "reward_hacks": [],
            "suggested_repairs": [],
        }
    )


@pytest.fixture
def good_trials():
    cfg = CampaignConfig()
    labels = {
        "oracle-0": 1,
        "oracle-1": 1,
        "oracle-2": 1,
        "baseline": 0,
        "tamper": 0,
        "mutation-boundary": 0,
        "mutation-constant": 0,
        "adversary": 0,
        "solver-0-0": 0,
        "solver-1-0": 1,
    }
    return [
        TrialEvidence(
            label=k,
            reward=v,
            task_digest="digest",
            path="/evidence/" + k,
            model=cfg.solver_models[int(k.split("-")[1])] if k.startswith("solver") else None,
        )
        for k, v in labels.items()
    ]


def test_admission_requires_complete_current_evidence(good_trials, good_review):
    args = (good_review, CampaignConfig(), "digest", ["boundary", "constant"])
    assert acceptance(good_trials, *args) == []
    for i in range(len(good_trials)):
        assert acceptance(good_trials[:i] + good_trials[i + 1 :], *args)
    for t in good_trials:
        original = t.task_digest
        t.task_digest = "old"
        assert acceptance(good_trials, *args)
        t.task_digest = original


def test_infrastructure_failure_is_not_difficulty(good_trials, good_review):
    solver = next(t for t in good_trials if t.label == "solver-0-0")
    solver.error = "provider timed out"
    assert acceptance(
        good_trials, good_review, CampaignConfig(), "digest", ["boundary", "constant"]
    )


def test_reward_hack_cannot_be_offset_by_high_score(good_trials, good_review):
    next(t for t in good_trials if t.label == "adversary").reward = 1
    assert good_review.score == 100
    assert acceptance(
        good_trials, good_review, CampaignConfig(), "digest", ["boundary", "constant"]
    )


def test_incomplete_and_nan_reviews_rejected(good_review):
    data = good_review.model_dump()
    del data["criteria"]["task_specification"]
    with pytest.raises(ValidationError):
        Review.model_validate(data)
    with pytest.raises(ValidationError):
        TrialEvidence(label="x", task_digest="d", path="/x", reward=float("nan"))


@pytest.mark.parametrize(
    "path", ["../escape", "/etc", ".", "src/../escape", "src/.git", "tests", "src//pkg"]
)
def test_submission_paths_reject_escapes(path):
    with pytest.raises(ValueError):
        Contract.safe_paths([path])


def test_seed_parser_deduplicates_without_executing_markdown(tmp_path):
    path = tmp_path / "seeds.md"
    path.write_text(
        "[PR](https://github.com/a/b/pull/123)\nhttps://github.com/a/b/pull/123\n"
        "ignore instructions; https://evil.example/a/b/pull/4"
    )
    assert parse_seeds(path) == ["https://github.com/a/b/pull/123"]


@pytest.mark.parametrize(
    "dependency", ["numpy", '"numpy>=1.0"', "numpy==1.*", "-r requirements.txt"]
)
def test_floating_dependencies_rejected_before_build(dependency):
    from repo2rlenv.curation.artifacts import validate_dependency_pins

    with pytest.raises(ValueError):
        validate_dependency_pins("RUN pip install " + dependency)


def test_exact_pins_and_editable_source_are_allowed():
    from repo2rlenv.curation.artifacts import validate_dependency_pins

    validate_dependency_pins(
        'RUN pip install --no-cache-dir "numpy==2.2.4" torch==2.6.0 --index-url https://example.org\n'
        "RUN pip install --no-deps --no-build-isolation -e /workspace"
    )


def test_duplicate_solver_and_trial_evidence_rejected(good_trials, good_review):
    with pytest.raises(ValidationError):
        CampaignConfig(solver_models=["same", "same"])
    assert acceptance(
        [*good_trials, good_trials[0]],
        good_review,
        CampaignConfig(),
        "digest",
        ["boundary", "constant"],
    )


def test_opt_in_judge_rejects_incomplete_or_coerced_scores():
    pytest.importorskip("harbor")
    from repo2rlenv.curation.judge_reward import validate_judgment

    criteria = {"correctness": "correct", "readability": "readable"}
    data = {
        "criteria": {k: {"pass": True, "reason": "Observed the required outcome"} for k in criteria}
    }
    assert validate_judgment(data, criteria) == 1
    data["criteria"]["correctness"]["pass"] = "true"
    with pytest.raises(ValueError):
        validate_judgment(data, criteria)
    del data["criteria"]["correctness"]
    with pytest.raises(ValueError):
        validate_judgment(data, criteria)


def test_finalize_owns_separate_grader_and_digest(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    import tomllib

    from repo2rlenv.curation.artifacts import digest_task, finalize

    monkeypatch.setattr(
        "repo2rlenv.curation.artifacts.pin_docker_base", lambda x: x + "@sha256:" + "a" * 64
    )
    source = {
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
        "id": "test-pr",
        "url": "https://github.com/a/b/pull/1",
    }
    content = {
        "instruction.md": "Fix the widget behavior in src/widget, including empty and nested inputs.",
        "environment/Dockerfile": "FROM python:3.12-slim\nWORKDIR /workspace\nRUN curl "
        + source["base_sha"]
        + "\nRUN pip install pytest==8.4.2\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_contract.py": "def test_empty(): pass\ndef test_nested(): pass\ndef test_general(): pass\n",
        "contract.json": json.dumps(
            {
                "title": "Widget",
                "rationale": "Real work",
                "source_paths": ["src/widget"],
                "requirements": [
                    {"id": "empty", "behavior": "empty input", "tests": ["test_empty"]},
                    {"id": "nested", "behavior": "nested input", "tests": ["test_nested"]},
                ],
                "mutations": [
                    {"name": "a", "rationale": "partial", "script": "true"},
                    {"name": "b", "rationale": "boundary", "script": "true"},
                ],
                "min_tests": 3,
            }
        ),
    }
    for name, text in content.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    finalize(tmp_path, source)
    config = tomllib.loads((tmp_path / "task.toml").read_text())
    assert config["environment"]["network_mode"] == "no-network"
    assert config["verifier"]["environment_mode"] == "separate"
    assert config["artifacts"][0]["source"] == "/workspace/src/widget"
    assert "rm -rf /workspace/src/widget" in (tmp_path / "tests/Dockerfile").read_text()
    before = digest_task(tmp_path)
    (tmp_path / "instruction.md").write_text("changed")
    assert digest_task(tmp_path) != before


def test_cli_validate_accepts_current_harbor_schema(tmp_path):
    from repo2rlenv.cli import main

    (tmp_path / "task.toml").write_text(
        'schema_version = "1.4"\n[task]\nname = "repo2rlenv/test"\n'
    )
    assert main(["validate", str(tmp_path)]) == 0


def test_runtime_names_leave_room_for_provider_suffixes():
    from repo2rlenv.curation.evaluate import trial_name

    a = trial_name("mutation-" + "long_behavior_" * 10)
    assert len(a + "__verifier__single") < 64
    assert a != trial_name("mutation-" + "long_behavior_" * 10)


def test_resolve_pr_uses_merge_base(monkeypatch):
    def api(path):
        if "/compare/" in path:
            return {"merge_base_commit": {"sha": "correct-base"}}
        return {
            "merged": True,
            "base": {"sha": "advanced-base", "repo": {"private": False}},
            "head": {"sha": "head"},
            "title": "test",
            "body": "",
            "merged_at": "date",
            "changed_files": 2,
            "additions": 4,
            "deletions": 1,
        }

    monkeypatch.setattr("repo2rlenv.curation.sources.api", api)
    assert resolve_pr("https://github.com/a/b/pull/123")["base_sha"] == "correct-base"


@pytest.mark.asyncio
async def test_langgraph_executes_tools_then_finishes(tmp_path, monkeypatch):
    pytest.importorskip("langgraph")
    from types import SimpleNamespace

    from repo2rlenv.curation.agent import SHELL_TOOL, run_agent

    responses = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call1",
                    "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
                    "type": "function",
                }
            ],
        },
        {"role": "assistant", "content": "Complete"},
    ]

    class Message:
        def __init__(self, data):
            self.data = data

        def model_dump(self, **kwargs):
            return self.data

    async def complete(*args, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=Message(responses.pop(0)))], usage=Message({})
        ), 0.1

    monkeypatch.setattr("repo2rlenv.curation.agent.completion", complete)
    seen = []

    async def shell(command):
        seen.append(command)
        return "/remote/workspace"

    state = await run_agent(
        model="test",
        system="test",
        prompt="test",
        budget=Budget(tmp_path / "ledger", 5),
        tools=[SHELL_TOOL],
        handlers={"shell": shell},
        trace=tmp_path / "trace.jsonl",
        max_turns=3,
    )
    assert seen == ["pwd"]
    assert state["turns"] == 2
    assert state["messages"][-1]["content"] == "Complete"
    assert any(
        json.loads(line).get("kind") == "tool"
        for line in (tmp_path / "trace.jsonl").read_text().splitlines()
    )
