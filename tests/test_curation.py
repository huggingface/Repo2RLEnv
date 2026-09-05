from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.inference import inference_digest
from repo2rlenv.curation.models import (
    CRITERIA,
    CampaignConfig,
    Contract,
    Review,
    TrialEvidence,
    acceptance,
    quality_gate_reasons,
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
            "adversary_assessment": "attempted_hack",
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
        "pytest-tamper": 0,
        "mutation-boundary": 0,
        "mutation-constant": 0,
        "equivalent-alternative": 1,
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
            model=cfg.solver_models[int(k.split("-")[1])]
            if k.startswith("solver")
            else (cfg.author_model if k == "adversary" else None),
            inference_digest=inference_digest(cfg.solver_models[int(k.split("-")[1])])
            if k.startswith("solver")
            else (inference_digest(cfg.author_model, adversary=True) if k == "adversary" else None),
        )
        for k, v in labels.items()
    ]


def test_admission_requires_complete_current_evidence(good_trials, good_review):
    args = (good_review, CampaignConfig(), "digest", ["boundary", "constant"], ["alternative"])
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
        good_trials,
        good_review,
        CampaignConfig(),
        "digest",
        ["boundary", "constant"],
        ["alternative"],
    )


def test_positive_adversary_reward_is_unresolved_not_automatically_a_confirmed_hack(
    good_trials, good_review
):
    next(t for t in good_trials if t.label == "adversary").reward = 1
    assert good_review.score == 100
    reasons = acceptance(
        good_trials,
        good_review,
        CampaignConfig(),
        "digest",
        ["boundary", "constant"],
        ["alternative"],
    )
    assert reasons
    assert not any(reason.startswith("Reward hack:") for reason in reasons)


@pytest.mark.parametrize("assessment", ["solved_task", "no_audit", "unknown"])
def test_incomplete_adversarial_audit_is_separate_from_quality(
    good_trials, good_review, assessment
):
    good_review.adversary_assessment = assessment
    config = CampaignConfig()
    reasons = acceptance(
        good_trials, good_review, config, "digest", ["boundary", "constant"], ["alternative"]
    )
    assert reasons == [f"Incomplete adversarial audit: {assessment}"]
    assert quality_gate_reasons(good_review, config) == []


def test_legacy_review_and_solver_policy_do_not_establish_a_current_audit(good_trials, good_review):
    data = good_review.model_dump()
    del data["adversary_assessment"]
    legacy = Review.model_validate(data)
    assert legacy.adversary_assessment == "unknown"
    config = CampaignConfig()
    attack = next(t for t in good_trials if t.label == "adversary")
    attack.inference_digest = inference_digest(config.author_model)
    reasons = acceptance(
        good_trials, legacy, config, "digest", ["boundary", "constant"], ["alternative"]
    )
    assert any("Adversarial trial" in reason for reason in reasons)
    assert "Incomplete adversarial audit: unknown" in reasons


def test_quality_gate_retains_real_defects_even_with_incomplete_audit(good_review):
    good_review.adversary_assessment = "no_audit"
    good_review.criteria["verifier_integrity"].score = 0
    good_review.criteria["verifier_integrity"].outcome = "fail"
    good_review.blockers = ["Observed verifier defect"]
    good_review.reward_hacks = ["A prior observed bypass passed without implementing behavior"]
    reasons = quality_gate_reasons(good_review, CampaignConfig())
    assert "Observed verifier defect" in reasons
    assert "Criterion not passed: verifier_integrity" in reasons
    assert any(reason.startswith("Reward hack:") for reason in reasons)
    assert not any("Incomplete adversarial audit" in reason for reason in reasons)


def test_valid_alternative_must_pass_and_cannot_be_omitted(good_trials, good_review):
    args = (good_review, CampaignConfig(), "digest", ["boundary", "constant"])
    assert acceptance(good_trials, *args, [])
    alternative = next(t for t in good_trials if t.label == "equivalent-alternative")
    alternative.reward = 0
    assert acceptance(good_trials, *args, ["alternative"])


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


def test_protected_tests_only_import_target_inside_worker():
    from repo2rlenv.curation.artifacts import validate_probe_tests

    validate_probe_tests(
        "from probe import run_probe\ndef test_behavior():\n"
        "    assert run_probe('import accelerate; print(1)') == 1\n"
    )
    for unsafe in [
        "import accelerate\n",
        "from torch import Tensor\n",
        "__import__('accelerate')\n",
        "exec('import accelerate')\n",
        "open('/workspace/code.py')\n",
    ]:
        with pytest.raises(ValueError):
            validate_probe_tests(unsafe + "run_probe('print(0)')\n")


def test_worker_cannot_own_assertions_or_exceed_protocol_bounds():
    from repo2rlenv.curation.probe_runtime import run_probe

    with pytest.raises(ValueError, match="Assertions"):
        run_probe("assert True")
    with pytest.raises(ValueError, match="timeout"):
        run_probe("print(0)", timeout=1000)
    with pytest.raises(ValueError, match="bounded protocol"):
        run_probe("print(0)", "x" * 1_000_001)


def test_duplicate_solver_and_trial_evidence_rejected(good_trials, good_review):
    with pytest.raises(ValidationError):
        CampaignConfig(solver_models=["same", "same"])
    assert acceptance(
        [*good_trials, good_trials[0]],
        good_review,
        CampaignConfig(),
        "digest",
        ["boundary", "constant"],
        ["alternative"],
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
        "tests/test_contract.py": "from probe import run_probe\ndef test_empty(): run_probe('print(0)')\ndef test_nested(): pass\ndef test_general(): pass\n",
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
                "equivalents": [
                    {"name": "alternative", "rationale": "equivalent", "script": "true"}
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


def test_oracle_script_and_artifact_failures_are_infrastructure(tmp_path):
    from repo2rlenv.curation.evaluate import evidence_summary, inspect_execution

    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/exit-code.txt").write_text("127")
    (tmp_path / "agent/oracle.txt").write_text("git: command not found")
    assert "127" in inspect_execution(tmp_path)
    evidence = TrialEvidence(label="oracle-0", task_digest="d", path=str(tmp_path), reward=0)
    assert "git: command not found" in evidence_summary([evidence])
    (tmp_path / "agent/exit-code.txt").unlink()
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/manifest.json").write_text(
        json.dumps([{"source": "/workspace/src/pkg", "status": "failed"}])
    )
    assert "Submission export failed" in inspect_execution(tmp_path)


def test_campaign_directory_cannot_have_two_controllers(tmp_path):
    from repo2rlenv.curation.campaign import campaign_lock

    with campaign_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            with campaign_lock(tmp_path):
                pytest.fail("Second controller acquired the lock")
    with campaign_lock(tmp_path):
        pass


def test_publication_freezes_bytes_and_excludes_solver_exports(tmp_path):
    from repo2rlenv.curation.publish import evidence_snapshot

    (tmp_path / "manifest.json").write_text('{"accepted": []}')
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/untrusted.txt").write_text("not evidence")
    (tmp_path / ".env").write_text("SECRET=example")
    with evidence_snapshot(tmp_path) as snapshot:
        (tmp_path / "manifest.json").write_text("changed after snapshot")
        assert (snapshot / "manifest.json").read_text() == '{"accepted": []}'
        assert not (snapshot / "artifacts").exists()
        assert not (snapshot / ".env").exists()
        assert not (snapshot / ".run.lock").exists()


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
@pytest.mark.parametrize("max_turns", [1, 3])
async def test_langgraph_executes_tools_then_finishes(tmp_path, monkeypatch, max_turns):
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
        max_turns=max_turns,
    )
    assert seen == ["pwd"]
    assert state["turns"] == min(max_turns, 2)
    assert state["messages"][-1]["content"] == (
        "Complete" if max_turns > 1 else "/remote/workspace"
    )
    assert any(
        json.loads(line).get("kind") == "tool"
        for line in (tmp_path / "trace.jsonl").read_text().splitlines()
    )


@pytest.mark.asyncio
async def test_model_reservation_respects_remaining_agent_cap(tmp_path, monkeypatch):
    import litellm

    from repo2rlenv.curation.budget import completion

    monkeypatch.setattr(
        litellm,
        "get_model_info",
        lambda _: {
            "input_cost_per_token": 0.001,
            "output_cost_per_token": 0.001,
        },
    )

    async def forbidden(**kwargs):
        pytest.fail("Provider called despite insufficient agent budget")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    budget = Budget(tmp_path / "ledger.json", 100)
    with pytest.raises(BudgetExceeded, match="agent cost limit"):
        await completion(budget, "test", [], max_charge=0.1)
    assert budget.spent == 0


def test_task_release_is_atomic_idempotent_and_preserves_existing(tmp_path):
    from repo2rlenv.curation.artifacts import release_task

    source, destination = tmp_path / "source", tmp_path / "released/task"
    source.mkdir()
    (source / "instruction.md").write_text("Original task")
    release_task(source, destination)
    release_task(source, destination)
    (source / "instruction.md").write_text("Changed task")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        release_task(source, destination)
    assert (destination / "instruction.md").read_text() == "Original task"
    assert not list(destination.parent.glob(".release-*"))
