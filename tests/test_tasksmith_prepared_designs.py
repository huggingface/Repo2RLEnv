"""Prepared designs save authoring only after exact-source, fresh-profile validation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.tasksmith import runner as runner_module
from repo2rlenv.tasksmith.author.artifact import canonical_digest
from repo2rlenv.tasksmith.models import (
    Design,
    Options,
    Panel,
    PreparedDesign,
    PreparedProfile,
    Profile,
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = {
        "url": "https://github.com/example/project/pull/7",
        "repo": "https://github.com/example/project",
        "head": "b" * 40,
        "base": "a" * 40,
        "source_diff": "diff --git a/src/core.py b/src/core.py\n--- a/src/core.py\n+++ b/src/core.py\n@@ -1 +1 @@\n-value=1\n+value=2\n",
        "source_files": ["src/core.py"],
        "source_operations": [{"path": "src/core.py", "operation": "modified"}],
        "workspace_strategy": "head_minus_source_patch",
    }
    identity = {key: source[key] for key in ("url", "head", "source_diff")}
    source["id"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    profile = Profile(
        reasoning="Preserve this exact offline source-era dependency profile.",
        resource="cpu",
        options={
            "source_paths": ["src"],
            "test_paths": ["tests"],
            "test_selectors": ["tests/test_core.py"],
            "dependencies": ["pytest==8.4.2"],
        },
        dependency_inputs=["pyproject.toml"],
        upstream_test_rationale="The selected offline test checks the public source behavior.",
    )
    design = Design(
        instruction="Restore the documented value returned by the public core function. " * 3,
        requirements=[
            {
                "behavior": "Return the documented public value.",
                "verification": "Compare against an independently specified expected value.",
                "source_evidence": "src/core.py",
            }
        ],
        verifier_rationale="Check the real function using an independent constant expectation.",
        additional_tests="def test_value():\n    from core import value\n    assert value() == 2\n",
        wrong_solution_ideas=["Return a constant zero."],
        valid_alternative_ideas=["Compute the same result using an equivalent expression."],
    )
    binding = {
        **{key: source[key] for key in ("url", "head", "base", "workspace_strategy")},
        "source_diff_sha256": hashlib.sha256(source["source_diff"].encode()).hexdigest(),
    }
    options = Options(
        prepared_profiles={source["id"]: PreparedProfile(**binding, profile=profile)},
        prepared_designs={
            source["id"]: PreparedDesign(
                **binding,
                profile_sha256=canonical_digest(profile.model_dump(mode="json")),
                design=design,
            )
        },
        bootstrap_hints={
            source["repo"]: {
                "ref": "c" * 40,
                "base_image": "python:3.12-slim",
                "dependencies": ["unrelated==1.0"],
                "scope": "General repository hint only.",
            }
        },
    )
    campaign = tmp_path / "campaign"
    BudgetLedger(campaign / "budget.sqlite3", limit_usd="20")
    runner = runner_module.Tasksmith(tmp_path / "run", campaign, options, tmp_path / "runtime.whl")
    monkeypatch.setattr(runner_module, "resolve_llm_api_key", lambda *args: "test-only")
    monkeypatch.setattr(runner_module, "check_runtime_wheel", lambda *args: "frozen-test-runtime")
    monkeypatch.setattr(
        "repo2rlenv.tasksmith.author.external_agent.runtime_path", lambda *args: tmp_path
    )
    return runner, source, profile, design


def stages(
    monkeypatch, setup, *, bootstrap_failures=0, construction_failures=0, changed_profile=False
):
    runner, _source, profile, design = setup
    calls = []
    bootstraps = constructions = 0

    def remote(root, stage, payload):
        nonlocal bootstraps, constructions
        calls.append(("remote", stage, deepcopy(payload)))
        if payload["stage"] == "inspect":
            return {"status": "completed", "value": {"checkout": "/remote/checkout"}}
        if payload["stage"] == "bootstrap":
            bootstraps += 1
            if bootstraps <= bootstrap_failures:
                return {"status": "failed", "error": "Fresh dependency failure"}
            return {
                "status": "completed",
                "value": {
                    "readiness": {"tests.test_core::test_value": "passed"},
                    "dependency_cache": {"cache_hit": False},
                    "image": "fresh-image",
                },
            }
        if payload["stage"] == "construct":
            constructions += 1
            assert payload["ready"]["image"] == "fresh-image"
            if constructions <= construction_failures:
                return {"status": "failed", "error": "Actual construction contrast failed"}
            return {"status": "completed", "local": str(root), "value": {"task_relative": "task"}}
        raise AssertionError("Prepared inputs must skip unrelated dependency preseed")

    def author(root, stage, schema, inputs, checkout, *, validate=None):
        calls.append(("author", stage, deepcopy(inputs)))
        if schema is Profile:
            value = profile.model_copy(deep=True)
            if changed_profile:
                value.options.dependencies = ["pytest==8.4.1"]
            asyncio.run(validate(value))
            return value
        assert schema is Design
        value = design.model_copy(deep=True)
        value.instruction += "\nRevised after the observed fresh failure."
        return value

    def quality(root, current_source, constructed):
        calls.append(("quality", "fresh", None))
        return {"status": "usable"}

    monkeypatch.setattr(runner, "remote", remote)
    monkeypatch.setattr(runner, "author", author)
    monkeypatch.setattr(runner, "review_candidate", quality)
    return calls


def run(setup, **kwargs):
    runner, source, _, _ = setup
    return runner.run(
        Panel(name="prepared-design", prs=[source["url"]]), source_records=[source], **kwargs
    )


def reuse_receipts(setup):
    runner, source, _, _ = setup
    root = runner.directory / "candidates" / source["id"] / "prepared-design"
    return [json.loads(p.read_text()) for p in root.glob("*.json")]


def test_exact_design_skips_author_but_runs_fresh_bootstrap_construction_and_quality(
    monkeypatch, setup
):
    calls = stages(monkeypatch, setup)
    result = run(setup)
    assert [(kind, stage) for kind, stage, _ in calls] == [
        ("remote", "inspect"),
        ("remote", "bootstrap-1"),
        ("remote", "construct-1"),
        ("quality", "fresh"),
    ]
    assert calls[2][2]["design"] == setup[3].model_dump()
    assert result["usable"] == 1
    [receipt] = reuse_receipts(setup)
    assert receipt["status"] == "reused" and receipt["bootstrap_attempt"] == 1
    assert receipt["profile_sha256"] == canonical_digest(setup[2].model_dump(mode="json"))
    assert receipt["readiness_sha256"] == canonical_digest(result["candidates"][0]["ready"])


@pytest.mark.parametrize(
    "field", ["url", "head", "base", "source_diff_sha256", "workspace_strategy"]
)
def test_source_binding_mismatch_fails_before_any_paid_work(monkeypatch, setup, field):
    runner, source, _, _ = setup
    prepared = runner.options.prepared_designs[source["id"]]
    setattr(prepared, field, "wrong" if field == "workspace_strategy" else "c" * 64)
    calls = stages(monkeypatch, setup)
    with pytest.raises(ValueError, match="Prepared design differs from the frozen PR source"):
        run(setup)
    assert calls == []


@pytest.mark.parametrize(
    "change", ["profile_digest", "profile_content", "missing_profile", "invalid_design"]
)
def test_inconsistent_seed_is_rejected_before_remote_preseed(monkeypatch, setup, change):
    runner, source, _, _ = setup
    prepared = runner.options.prepared_designs[source["id"]]
    if change == "profile_digest":
        prepared.profile_sha256 = "c" * 64
    elif change == "profile_content":
        runner.options.prepared_profiles[source["id"]].profile.options.dependencies.append(
            "extra==1"
        )
    elif change == "missing_profile":
        runner.options.prepared_profiles.clear()
    else:
        prepared.design.additional_tests = (
            "import inspect\ndef test_bad():\n    assert inspect.getsource(module)\n"
        )
    calls = stages(monkeypatch, setup)
    with pytest.raises(ValueError):
        run(setup)
    assert calls == []


def test_unknown_source_id_cannot_allocate(monkeypatch, setup):
    runner, source, _, _ = setup
    runner.options.prepared_designs["c" * 12] = runner.options.prepared_designs.pop(source["id"])
    calls = stages(monkeypatch, setup)
    with pytest.raises(ValueError, match="outside the selected sources"):
        run(setup)
    assert calls == []


@pytest.mark.parametrize("mode", ["imported", "prepared_task"])
def test_generated_task_and_design_reuse_are_not_combined(monkeypatch, setup, tmp_path, mode):
    runner, source, _, _ = setup
    calls = stages(monkeypatch, setup)
    if mode == "imported":
        runner.options.prepared_profiles.clear()
        runner.imported[source["id"]] = {"task": "unused"}
    with pytest.raises(ValueError, match="prepared design or generated task"):
        run(
            setup,
            **({"prepared_task": tmp_path / "not-materialized"} if mode == "prepared_task" else {}),
        )
    assert calls == []


def test_bootstrap_profile_repair_falls_back_to_author_with_clear_receipt(monkeypatch, setup):
    calls = stages(monkeypatch, setup, bootstrap_failures=1, changed_profile=True)
    result = run(setup)
    assert [(kind, stage) for kind, stage, _ in calls] == [
        ("remote", "inspect"),
        ("remote", "bootstrap-1"),
        ("author", "investigate-1"),
        ("remote", "bootstrap-2"),
        ("author", "design-0"),
        ("remote", "construct-1"),
        ("quality", "fresh"),
    ]
    [receipt] = reuse_receipts(setup)
    assert receipt["status"] == "profile_changed" and receipt["bootstrap_attempt"] == 2
    assert result["candidates"][0]["design"] != setup[3].model_dump()
    assert "author a new design" in (setup[0].directory / "events.jsonl").read_text()


def test_failed_construction_uses_existing_bounded_author_repair(monkeypatch, setup):
    calls = stages(monkeypatch, setup, construction_failures=1)
    result = run(setup)
    authored = [(stage, inputs) for kind, stage, inputs in calls if kind == "author"]
    assert len(authored) == 1 and authored[0][0] == "design-1"
    assert authored[0][1]["previous_design"] == setup[3].model_dump()
    assert authored[0][1]["previous_failure"]["error"] == "Actual construction contrast failed"
    assert result["candidates"][0]["design_attempt"] == 2
    assert len(reuse_receipts(setup)) == 1


def test_prepared_design_counts_toward_the_construction_attempt_limit(monkeypatch, setup):
    setup[0].options.max_stage_attempts = 3
    calls = stages(monkeypatch, setup, construction_failures=3)
    result = run(setup)
    assert [stage for kind, stage, _ in calls if kind == "author"] == ["design-1", "design-2"]
    assert result["candidates"][0]["status"] == "construction_failed"
    assert result["candidates"][0]["design_attempt"] == 3
    assert len(reuse_receipts(setup)) == 1
    assert not any(kind == "quality" for kind, _, _ in calls)


def test_retry_with_unchanged_profile_still_requires_fresh_readiness(monkeypatch, setup):
    calls = stages(monkeypatch, setup, bootstrap_failures=1)
    assert run(setup)["usable"] == 1
    assert [stage for kind, stage, _ in calls if kind == "author"] == ["investigate-1"]
    [receipt] = reuse_receipts(setup)
    assert receipt["status"] == "reused" and receipt["bootstrap_attempt"] == 2


def test_no_design_reuse_when_bootstrap_never_passes(monkeypatch, setup):
    setup[0].options.max_stage_attempts = 2
    calls = stages(monkeypatch, setup, bootstrap_failures=2)
    result = run(setup)
    assert not any(stage.startswith("design") for _, stage, _ in calls)
    assert reuse_receipts(setup) == []
    assert result["candidates"][0]["status"] == "bootstrap_failed"


def test_changed_design_cannot_resume_existing_frozen_run(monkeypatch, setup):
    calls = stages(monkeypatch, setup)
    run(setup)
    before = len(calls)
    runner, source, _, _ = setup
    runner.options.prepared_designs[source["id"]].design.instruction += "\nChanged requirements."
    with pytest.raises(ValueError, match="configuration changed"):
        run(setup)
    assert len(calls) == before


def test_old_options_without_design_seed_keep_ordinary_authoring(monkeypatch, setup):
    runner, _, _, _ = setup
    old = runner.options.model_dump(mode="json")
    del old["prepared_designs"]
    runner.options = Options.model_validate(old)
    calls = stages(monkeypatch, setup)
    assert run(setup)["usable"] == 1
    assert [stage for kind, stage, _ in calls if kind == "author"] == ["design-0"]
    assert reuse_receipts(setup) == []
