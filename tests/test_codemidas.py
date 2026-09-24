from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.pipelines.recipes.codemidas.models import Feature, Verifier
from repo2rlenv.pipelines.recipes.codemidas.source import (
    implementation_pair,
    remove_bodies,
    validate_assertions,
)
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.tasksmith.author.budget import AuthorBudget
from repo2rlenv.tasksmith.author.openai_agent import model_name, run_openai_agent, usage_cost


def feature():
    return Feature(
        title="Implement public list transformation",
        instruction="Public behavior. " * 20,
        symbols=[{"path": "lib/core.py", "qualified_name": "transform"}],
        requirements=[
            {"id": f"R{i}", "behavior": "Defined observable behavior."} for i in range(1, 4)
        ],
        rationale="A meaningful public feature with multiple edge cases.",
    )


def test_remove_only_selected_methods_and_preserve_decorators():
    code = "class A:\n    @staticmethod\n    def f(x):\n        return x + 1\n\ndef g():\n    return 99\n"
    starter = remove_bodies(code, ["A.f"])
    assert "@staticmethod" in starter and "return 99" in starter
    assert "return x + 1" not in starter
    with pytest.raises(ValueError, match="Unknown"):
        remove_bodies(code, ["A.missing"])


def test_all_verifier_requirements_are_visible_to_the_learner():
    task = feature()
    task.requirements[0].behavior = "Reject project names containing two consecutive underscores."
    instruction = task.task_instruction()
    assert instruction.startswith(task.instruction.rstrip())
    assert all(item.behavior in instruction for item in task.requirements)
    assert task.rationale not in instruction


def test_verifier_timeout_is_retained_repair_feedback_not_baseline_success(tmp_path, monkeypatch):
    import subprocess

    from repo2rlenv.pipelines.recipes.codemidas import worker
    from repo2rlenv.spec.recipe_options import CodeMidasOptions

    base = tmp_path / "generation/base/lib"
    base.mkdir(parents=True)
    (base / "core.py").write_text("def transform(x):\n    return sorted(set(x))\n")
    verifier = Verifier(
        test_code="\n".join(
            f"def test_{i}():\n    assert True  # " + "context " * 10 for i in range(3)
        ),
        assertions=[
            {
                "test": f"test_{i}",
                "requirements": [f"R{i + 1}"],
                "observation": "Observed through a recorded public API execution.",
            }
            for i in range(3)
        ],
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker start", 90, output=b"started test")

    monkeypatch.setattr(worker, "test_image", timeout)
    output = tmp_path / "evaluation"
    result = worker.evaluate(
        {
            "generation": str(tmp_path / "generation"),
            "feature": feature().model_dump(),
            "verifier": verifier.model_dump(),
            "image_digest": "sha256:fixture",
        },
        CodeMidasOptions(source_paths=["lib"]),
        output,
    )
    assert result["contrast"] is None
    assert result["error_type"] == "test_timeout"
    assert result["phase"] == "reference"
    assert result["timeout_output"] == "started test"
    assert json.loads((output / "evaluation.json").read_text()) == result


def test_implementation_pair_refuses_traversal_and_restores_original(tmp_path):
    (tmp_path / "lib").mkdir()
    original = b"def transform(x):\n    return sorted(set(x))\n"
    (tmp_path / "lib/core.py").write_bytes(original)
    task = feature()
    defective, reference = implementation_pair(tmp_path, task, ["lib"])
    assert reference == {"lib/core.py": original}
    assert b"NotImplementedError" in defective["lib/core.py"]
    task.symbols[0].path = "../secret.py"
    with pytest.raises(ValueError):
        implementation_pair(tmp_path, task, ["lib"])


def test_assertion_map_rejects_uncovered_or_invented_requirements():
    verifier = Verifier(
        test_code="\n".join(
            f"def test_{i}():\n    assert True  # " + "context " * 10 for i in range(3)
        ),
        assertions=[
            {
                "test": f"test_{i}",
                "requirements": [f"R{i + 1}"],
                "observation": "Observed through a recorded public API execution.",
            }
            for i in range(3)
        ],
    )
    validate_assertions(feature(), verifier)
    verifier.assertions[0].requirements = ["R999"]
    with pytest.raises(ValueError, match="declared"):
        validate_assertions(feature(), verifier)


def test_prices_include_cache_and_refuse_unapproved_models():
    assert usage_cost(
        "gpt-6-sol",
        {
            "input_tokens": 1000,
            "output_tokens": 200,
            "input_tokens_details": {"cached_tokens": 500},
        },
    ) == Decimal("0.0031")
    with pytest.raises(ValueError):
        model_name("anthropic/claude-sonnet-4-6")
    with pytest.raises(ValueError):
        usage_cost("gpt-6-luna", {"input_tokens": 300000, "output_tokens": 1})


def test_responses_effect_is_metered_before_tool_execution(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1")
    budget = AuthorBudget(RunBudget(ledger, "cm-test", "1"), tmp_path / "costs", "design")
    data = {
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "output": [
            {
                "type": "function_call",
                "name": "submit_artifact",
                "call_id": "call-1",
                "arguments": '{"answer": 42}',
            }
        ],
    }

    async def create(**kwargs):
        assert ledger.status()["operations"][0]["status"] == "reserved"
        assert kwargs["model"] == "gpt-6-luna" and kwargs["store"] is False
        return SimpleNamespace(model_dump=lambda **_: data)

    async def submit_artifact(answer):
        assert ledger.status()["operations"][0]["status"] == "settled"
        assert answer == 42
        return "Artifact committed."

    result = asyncio.run(
        run_openai_agent(
            model="openai/gpt-6-luna",
            system="System",
            prompt="Task",
            budget=budget,
            tools=[],
            handlers={"submit_artifact": submit_artifact},
            trace=tmp_path / "trace.jsonl",
            max_turns=2,
            client=SimpleNamespace(responses=SimpleNamespace(create=create)),
        )
    )
    assert result["turns"] == 1
    assert ledger.status()["accounted_usd"] == "0.000020"


def test_unknown_provider_outcome_retains_reservation(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1")
    budget = AuthorBudget(RunBudget(ledger, "cm-test", "1"), tmp_path / "costs", "design")

    async def create(**kwargs):
        raise TimeoutError("Unknown outcome")

    with pytest.raises(TimeoutError):
        asyncio.run(
            run_openai_agent(
                model="openai/gpt-6-luna",
                system="System",
                prompt="Task",
                budget=budget,
                tools=[],
                handlers={},
                trace=tmp_path / "trace.jsonl",
                max_turns=2,
                client=SimpleNamespace(responses=SimpleNamespace(create=create)),
            )
        )
    assert ledger.status()["operations"][0]["status"] == "uncertain"


def test_malformed_tool_arguments_get_feedback_without_executing_handler(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1")
    budget = AuthorBudget(RunBudget(ledger, "cm-test", "1"), tmp_path / "costs", "design")
    calls = []

    async def create(**kwargs):
        turn = len(calls)
        calls.append(kwargs)
        if turn:
            assert "Invalid tool arguments" in kwargs["input"][-1]["output"]
        data = {
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "output": [
                {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": f"call-{turn}",
                    "arguments": json.dumps({"patch": {}} if not turn else {"command": "done"}),
                }
            ],
        }
        return SimpleNamespace(model_dump=lambda **_: data)

    executed = []

    async def shell(command):
        executed.append(command)
        return "Artifact committed."

    asyncio.run(
        run_openai_agent(
            model="openai/gpt-6-luna",
            system="System",
            prompt="Task",
            budget=budget,
            tools=[],
            handlers={"shell": shell},
            trace=tmp_path / "trace.jsonl",
            max_turns=2,
            client=SimpleNamespace(responses=SimpleNamespace(create=create)),
        )
    )
    assert executed == ["done"]
    assert ledger.status()["accounted_usd"] == "0.000040"
    assert ledger.status()["reserved_usd"] == "0.000000"


def test_cache_writes_are_billed_at_their_distinct_rate():
    assert usage_cost(
        "gpt-6-luna",
        {
            "input_tokens": 1000,
            "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 800},
        },
    ) == Decimal("0.000102")


def test_stateless_reasoning_replay_drops_null_sdk_fields(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1")
    budget = AuthorBudget(RunBudget(ledger, "cm", "1"), tmp_path / "costs", "design")
    calls = 0

    async def create(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            reasoning = next(item for item in kwargs["input"] if item.get("type") == "reasoning")
            assert "status" not in reasoning and "content" not in reasoning
            assert reasoning["encrypted_content"] == "opaque"
        data = {
            "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs",
                    "summary": [],
                    "status": None,
                    "content": None,
                    "encrypted_content": "opaque",
                },
                {"type": "function_call", "name": "next", "arguments": "{}", "call_id": str(calls)},
            ],
        }
        return SimpleNamespace(model_dump=lambda **_: data)

    async def next_tool():
        return "Read completed" if calls == 1 else "Artifact committed."

    asyncio.run(
        run_openai_agent(
            model="gpt-6-luna",
            system="System",
            prompt="Task",
            budget=budget,
            tools=[],
            handlers={"next": next_tool},
            trace=tmp_path / "trace.jsonl",
            max_turns=2,
            client=SimpleNamespace(responses=SimpleNamespace(create=create)),
        )
    )
    assert calls == 2


def test_curriculum_is_not_the_soundness_gate():
    from repo2rlenv.pipelines.recipes.codemidas.audit import curriculum

    assert curriculum([1, 1, 1, 1]) == "all_pass"
    assert curriculum([0, 0, 0, 0]) == "all_fail"
    assert curriculum([0, 1, 0, 1]) == "mixed"
    assert curriculum([None, 1]) == "incomplete"


def test_audit_indexes_all_changed_files_under_nested_workspace(tmp_path):
    from repo2rlenv.pipelines.recipes.codemidas.audit import evidence_index

    task = tmp_path / "task"
    original = task / "environment/source/library.py"
    original.parent.mkdir(parents=True)
    original.write_text("unchanged")
    runs = tmp_path / "workspace/campaign/audit"
    submitted = runs / "solve-0/job/trial/artifacts/workspace/library.py"
    submitted.parent.mkdir(parents=True)
    manifest = submitted.parents[1] / "manifest.json"
    manifest.write_text("{}")
    submitted.write_text("changed")
    index = evidence_index(task, runs)
    assert "attempts/solve-0/job/trial/artifacts/workspace/library.py" in index
    assert "task/environment/source/library.py" in index
    submitted.write_text("unchanged")
    assert set(evidence_index(task, runs)) == {"attempts/solve-0/job/trial/artifacts/manifest.json"}


def test_audit_points_to_changed_lines_without_replacing_submitted_evidence(tmp_path):
    from repo2rlenv.pipelines.recipes.codemidas.audit import evidence_index

    task, runs = tmp_path / "task", tmp_path / "audit"
    original = task / "environment/source/lib.py"
    submitted = runs / "solve-0/job/trial/artifacts/workspace/lib.py"
    original.parent.mkdir(parents=True)
    submitted.parent.mkdir(parents=True)
    original.write_text(
        "def public():\n    raise NotImplementedError\n\ndef other():\n    return 9\n"
    )
    replacement = "def public():\n    value = 3\n    return value\n\ndef other():\n    return 9\n"
    submitted.write_text(replacement)
    record = evidence_index(task, runs)["attempts/solve-0/job/trial/artifacts/workspace/lib.py"]
    assert record["changed_ranges"] == [
        {
            "kind": "replace",
            "starter_first_line": 2,
            "starter_line_count": 1,
            "submitted_first_line": 2,
            "submitted_line_count": 2,
        }
    ]
    assert record["path"] == str(submitted.resolve())
    assert submitted.read_text() == replacement


def test_incomplete_response_is_billed_but_never_executes_partial_tools(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1")
    budget = AuthorBudget(RunBudget(ledger, "cm", "1"), tmp_path / "costs", "design")
    calls, effects = [], []

    async def create(**request):
        calls.append(request)
        data = {
            "status": "incomplete" if len(calls) == 1 else "completed",
            "incomplete_details": {"reason": "max_messages"},
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "output": [
                {
                    "type": "function_call",
                    "name": "finish",
                    "call_id": str(len(calls)),
                    "arguments": json.dumps({"value": len(calls)}),
                }
            ],
        }
        return SimpleNamespace(model_dump=lambda **_: data)

    async def finish(value):
        effects.append(value)
        return "Artifact committed."

    asyncio.run(
        run_openai_agent(
            model="gpt-6-luna",
            system="System",
            prompt="Task",
            budget=budget,
            tools=[],
            handlers={"finish": finish},
            trace=tmp_path / "trace.jsonl",
            max_turns=3,
            client=SimpleNamespace(responses=SimpleNamespace(create=create)),
        )
    )
    assert effects == [2]
    assert len(calls) == 2
    assert ledger.status()["accounted_usd"] == "0.000040"
    assert not any(item.get("call_id") == "1" for item in calls[1]["input"])


def test_public_build_retains_only_empty_test_package_scaffolding(tmp_path):
    from repo2rlenv.pipelines.recipes.codemidas.sanitize import public_build_context, public_profile
    from repo2rlenv.spec.recipe_options import CodeMidasOptions

    base = tmp_path / "repo"
    (base / "library/tests").mkdir(parents=True)
    (base / "library/tests/__init__.py").write_text("SECRET = 'reference answer'")
    (base / "library/core.py").write_text("public = 1")
    (base / "LICENSE").write_text("Original license")
    options = CodeMidasOptions(source_paths=["library"])
    profile = public_profile(base, options, ["library/tests"])
    public = tmp_path / "public"
    public_build_context(base, profile, public)
    assert not (public / "source/library/tests").exists()
    assert (public / "source/LICENSE").read_text() == "Original license"
    assert "touch -- library/tests/__init__.py" in (public / "Dockerfile").read_text()
    assert "reference answer" not in (public / "Dockerfile").read_text()


def test_failed_response_trial_settles_only_fully_accounted_usage(tmp_path):
    from repo2rlenv.execution.harbor import TrialEvidence, responses_cost_is_complete

    result = tmp_path / "result.json"
    budget = {
        "operations": [{"status": "settled", "actual_micros": 20000}],
        "accounted_usd": "0.020000",
        "reserved_usd": "0.000000",
    }
    result.write_text(json.dumps({"agent_result": {"metadata": {"budget": budget}}}))
    evidence = TrialEvidence(None, "ProviderOutputError", result, 0.02)
    assert responses_cost_is_complete(evidence)
    budget["operations"].append({"status": "uncertain", "actual_micros": None})
    result.write_text(json.dumps({"agent_result": {"metadata": {"budget": budget}}}))
    assert not responses_cost_is_complete(evidence)


def test_audit_judgments_require_real_citations_and_agree_with_rewards(tmp_path):
    from repo2rlenv.pipelines.recipes.codemidas.audit import AuditReview, validate_review

    trace = tmp_path / "trace.jsonl"
    trace.write_text("one recorded command\nits recorded output\n")
    index = {f"attempts/solve-{i}/trace.jsonl": {"path": str(trace)} for i in range(4)}
    review = AuditReview(
        sound=True,
        exploit_confirmed=False,
        exploit_explanation="The recorded exploit attempt found no accessible reference.",
        judgments=[
            {
                "trial": f"solve-{i}",
                "outcome": "legitimate_success",
                "explanation": "The submitted implementation matches the required behavior.",
                "evidence": [f"attempts/solve-{i}/trace.jsonl:1-2"],
            }
            for i in range(4)
        ],
        issues=[],
    )
    attempts = [{"trial": f"solve-{i}", "reward": 1, "exception": None} for i in range(4)]
    attempts.append({"trial": "exploit", "reward": 0, "exception": None})
    validate_review(review, index, attempts)
    attempts[0]["reward"] = 0
    with pytest.raises(ValueError, match="contradicts"):
        validate_review(review, index, attempts)
    attempts[0]["reward"] = 1
    review.judgments[0].evidence = ["attempts/solve-0/trace.jsonl:1-99"]
    with pytest.raises(ValueError, match="line bounds"):
        validate_review(review, index, attempts)
    review.judgments[0].evidence = ["attempts/solve-0/invented.json"]
    with pytest.raises(ValueError, match="not in"):
        validate_review(review, index, attempts)


def test_stack_row_preserves_redacted_content_without_faking_blob_identity(tmp_path):
    from repo2rlenv.pipelines.recipes.codemidas.stack import (
        DATASET,
        materialize,
        provenance,
        validate_manifest,
    )

    row = {
        "dataset": DATASET,
        "dataset_revision": "a" * 40,
        "row": {
            "repo_path": "org/repo",
            "repo_id": 1,
            "commit_id": "b" * 40,
            "num_files": 1,
            "files": [
                {
                    "file_path": "lib.py",
                    "content": "# redacted code\n",
                    "content_id": "c" * 40,
                    "license_type": "permissive",
                    "detected_licenses": ["MIT"],
                }
            ],
        },
    }
    info = provenance(row, "inline")
    assert info["files"][0]["content_id"] == "c" * 40
    materialize(row, tmp_path / "source")
    assert (tmp_path / "source/lib.py").read_text() == "# redacted code\n"
    row["row"]["files"][0]["file_path"] = "../escape.py"
    with pytest.raises(ValueError):
        validate_manifest(row)


def test_reconstruction_dedup_uses_missing_code_not_anchor_or_instruction(tmp_path):
    from repo2rlenv.pipelines.recipes.codemidas.source import (
        existing_reconstructions,
        reconstruction_identity,
    )

    task = tmp_path / "task"
    (task / "solution/reference").mkdir(parents=True)
    (task / "environment/source").mkdir(parents=True)
    (task / "solution/reference/api.py").write_text("def run():\n    return 1\n")
    defective = b"def run():\n    raise NotImplementedError\n"
    (task / "environment/source/api.py").write_bytes(defective)
    (task / "task.toml").write_text(
        '[metadata.repo2env]\nrecipe="codemidas"\nrepository="owner/repo"\nsource_revision="abc"\n'
    )
    identity = reconstruction_identity("owner/repo", "abc", {"api.py": defective})
    assert existing_reconstructions(tmp_path) == {identity}
    assert reconstruction_identity("owner/repo", "other", {"api.py": defective}) != identity
    assert reconstruction_identity("owner/repo", "abc", {"api.py": b"other gap"}) != identity
