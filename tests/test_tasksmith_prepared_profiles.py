"""Exact-profile recovery still builds and validates the current PR remotely."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.tasksmith import runner as runner_module
from repo2rlenv.tasksmith.models import (
    BootstrapHint,
    Design,
    Options,
    Panel,
    PreparedProfile,
    Profile,
)


@pytest.fixture
def source():
    record = {
        "url": "https://github.com/example/project/pull/7",
        "repo": "https://github.com/example/project",
        "head": "b" * 40,
        "base": "a" * 40,
        "source_diff": "diff --git a/src/core.py b/src/core.py\n--- a/src/core.py\n+++ b/src/core.py\n@@ -1 +1 @@\n-value=1\n+value=2\n",
        "source_files": ["src/core.py"],
        "source_operations": [{"path": "src/core.py", "operation": "modified"}],
        "workspace_strategy": "head_minus_source_patch",
    }
    identity = {key: record[key] for key in ("url", "head", "source_diff")}
    record["id"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return record


@pytest.fixture
def profile():
    return Profile(
        reasoning="Preserve a complete, previously successful offline build profile.",
        resource="cpu",
        options={
            "source_paths": ["src"],
            "test_paths": ["tests"],
            "test_selectors": ["tests/test_core.py"],
            "dependencies": ["pytest==8.4.2"],
        },
        dependency_inputs=["pyproject.toml"],
        upstream_test_rationale="Offline regression tests exercise the selected public behavior.",
    )


@pytest.fixture
def prepared(source, profile):
    return PreparedProfile(
        **{key: source[key] for key in ("url", "head", "base", "workspace_strategy")},
        source_diff_sha256=hashlib.sha256(source["source_diff"].encode()).hexdigest(),
        profile=profile,
    )


@pytest.fixture
def runner(monkeypatch, tmp_path, source, prepared):
    campaign = tmp_path / "campaign"
    BudgetLedger(campaign / "budget.sqlite3", limit_usd="20")
    options = Options(
        prepared_profiles={source["id"]: prepared},
        bootstrap_hints={
            source["repo"]: BootstrapHint(
                ref="c" * 40,
                base_image="python:3.12-slim",
                dependencies=["unrelated-hint==1.0"],
                scope="Repository cache hint, not the selected PR recipe.",
            )
        },
    )
    value = runner_module.Tasksmith(tmp_path / "run", campaign, options, tmp_path / "runtime.whl")
    monkeypatch.setattr(runner_module, "resolve_llm_api_key", lambda *args: "test-only")
    monkeypatch.setattr(runner_module, "check_runtime_wheel", lambda *args: "frozen-test-runtime")
    monkeypatch.setattr(
        "repo2rlenv.tasksmith.author.external_agent.runtime_path", lambda *args: tmp_path
    )
    return value


def install_stages(monkeypatch, runner, profile, *, fail_bootstraps=0, links=()):
    calls = []
    bootstraps = 0

    def remote(root, stage, payload):
        nonlocal bootstraps
        calls.append(("remote", stage, deepcopy(payload)))
        if payload["stage"] == "inspect":
            return {
                "status": "completed",
                "value": {"checkout": "/remote/checkout", "snapshot_links": list(links)},
            }
        if payload["stage"] == "bootstrap":
            bootstraps += 1
            if bootstraps <= fail_bootstraps:
                return {"status": "failed", "error": "Fresh bootstrap dependency failure"}
            return {
                "status": "completed",
                "value": {
                    "readiness": {"tests.test_core::test_value": "passed"},
                    "dependency_cache": {"cache_hit": False},
                    "image": "fresh-image",
                },
            }
        if payload["stage"] == "construct":
            assert payload["ready"]["image"] == "fresh-image"
        return {"status": "completed", "local": str(root), "value": {"task_relative": "task"}}

    def author(root, stage, schema, inputs, checkout, *, validate=None):
        calls.append(("author", stage, deepcopy(inputs)))
        if schema is Profile:
            result = Profile.model_validate(profile.model_dump())
            asyncio.run(validate(result))
            return result
        assert schema is Design
        return Design(
            instruction="Restore the documented public value returned by the core module. " * 3,
            requirements=[
                {
                    "behavior": "Return the correct documented public value.",
                    "source_evidence": "src/core.py",
                    "verification": "Compare the public value against an independent expected value.",
                }
            ],
            verifier_rationale="Use the independently specified expected value from the contract.",
            wrong_solution_ideas=["Return a constant zero."],
            valid_alternative_ideas=["Compute the same value through an equivalent expression."],
        )

    def review(root, current_source, constructed):
        calls.append(("quality", "fresh", None))
        return {"status": "usable", "quality": {"fresh": True}}

    monkeypatch.setattr(runner, "remote", remote)
    monkeypatch.setattr(runner, "author", author)
    monkeypatch.setattr(runner, "review_candidate", review)
    return calls


def execute(runner, source):
    return runner.run(Panel(name="prepared", prs=[source["url"]]), source_records=[source])


def test_exact_profile_skips_investigation_and_hint_but_runs_fresh_stages(
    monkeypatch, runner, source, profile
):
    calls = install_stages(monkeypatch, runner, profile)
    result = execute(runner, source)
    assert [(kind, stage) for kind, stage, _ in calls] == [
        ("remote", "inspect"),
        ("remote", "bootstrap-1"),
        ("author", "design-0"),
        ("remote", "construct-1"),
        ("quality", "fresh"),
    ]
    assert calls[1][2]["profile"] == profile.model_dump()
    assert result["candidates"][0]["profile_attempt"] == 1
    assert result["candidates"][0]["ready"]["image"] == "fresh-image"


@pytest.mark.parametrize("field", ["url", "head", "base", "source_diff_sha256"])
def test_source_mismatch_stops_before_any_remote_dependency_work(
    monkeypatch, runner, source, profile, field
):
    prepared = runner.options.prepared_profiles[source["id"]]
    replacement = (
        "https://github.com/example/project/pull/8"
        if field == "url"
        else "d" * (64 if field == "source_diff_sha256" else 40)
    )
    setattr(prepared, field, replacement)
    calls = install_stages(monkeypatch, runner, profile)
    with pytest.raises(ValueError, match="differs from the frozen PR"):
        execute(runner, source)
    assert calls == []


@pytest.mark.parametrize("change", ["gpu", "uncovered", "hidden"])
def test_invalid_resource_or_source_coverage_cannot_allocate(
    monkeypatch, runner, source, profile, change
):
    if change == "gpu":
        runner.options.prepared_profiles[source["id"]].profile.resource = "gpu"
    elif change == "uncovered":
        profile.options.source_paths = ["other"]
    else:
        profile.options.public_exclude = ["src/core.py"]
    calls = install_stages(monkeypatch, runner, profile)
    with pytest.raises(ValueError):
        execute(runner, source)
    assert calls == []


def test_unknown_profile_id_is_rejected_before_remote_work(monkeypatch, runner, source, profile):
    runner.options.prepared_profiles["c" * 12] = runner.options.prepared_profiles.pop(source["id"])
    calls = install_stages(monkeypatch, runner, profile)
    with pytest.raises(ValueError, match="outside the selected sources"):
        execute(runner, source)
    assert calls == []


def test_imported_task_and_prepared_profile_are_not_combined(monkeypatch, runner, source, profile):
    runner.imported = {source["id"]: {"task": "unused"}}
    calls = install_stages(monkeypatch, runner, profile)
    with pytest.raises(ValueError, match="prepared profile or generated task"):
        execute(runner, source)
    assert calls == []


@pytest.mark.parametrize("field", ["test_cpus", "test_memory_mb"])
def test_prepared_resource_overflow_stops_before_remote_work(
    monkeypatch, runner, source, profile, field
):
    setattr(profile.options, field, {"test_cpus": 3, "test_memory_mb": 8192}[field])
    calls = install_stages(monkeypatch, runner, profile)
    with pytest.raises(ValueError, match=f"options.{field}.*exceeds worker allocation"):
        execute(runner, source)
    assert calls == []


def test_investigator_resource_overflow_cannot_build(monkeypatch, runner, source, profile):
    runner.options.prepared_profiles = {}
    runner.options.bootstrap_hints = {}
    profile.options.test_memory_mb = 8192
    calls = install_stages(monkeypatch, runner, profile)
    result = execute(runner, source)
    assert "exceeds worker allocation" in result["candidates"][0]["error"]
    assert [(kind, stage) for kind, stage, _ in calls] == [
        ("remote", "inspect"),
        ("author", "investigate-0"),
    ]
    assert calls[1][2]["requested_resources"]["worker_memory_mb"] == 4096


def test_document_links_are_still_validated_before_build(monkeypatch, runner, source, profile):
    calls = install_stages(monkeypatch, runner, profile, links=[{"path": "CONTRIBUTING.md"}])
    result = execute(runner, source)
    assert result["candidates"][0]["status"] == "incomplete"
    assert "identified document links" in result["candidates"][0]["error"]
    assert [(kind, stage) for kind, stage, _ in calls] == [("remote", "inspect")]


def test_fresh_bootstrap_failure_can_repair_within_original_attempt_limit(
    monkeypatch, runner, source, profile
):
    runner.options.max_stage_attempts = 2
    calls = install_stages(monkeypatch, runner, profile, fail_bootstraps=2)
    result = execute(runner, source)
    assert [(kind, stage) for kind, stage, _ in calls] == [
        ("remote", "inspect"),
        ("remote", "bootstrap-1"),
        ("author", "investigate-1"),
        ("remote", "bootstrap-2"),
    ]
    assert calls[2][2]["previous_profile"] == profile.model_dump()
    assert calls[2][2]["previous_failure"]["error"] == "Fresh bootstrap dependency failure"
    assert result["candidates"][0]["status"] == "bootstrap_failed"


def test_changed_inline_profile_invalidates_frozen_resume(monkeypatch, runner, source, profile):
    calls = install_stages(monkeypatch, runner, profile)
    execute(runner, source)
    before = len(calls)
    runner.options.prepared_profiles[source["id"]].profile.options.dependencies = ["pytest==8.4.1"]
    with pytest.raises(ValueError, match="configuration changed"):
        execute(runner, source)
    assert len(calls) == before


def test_default_pipeline_still_uses_hint_and_investigator(monkeypatch, runner, source, profile):
    runner.options.prepared_profiles = {}
    calls = install_stages(monkeypatch, runner, profile)
    result = execute(runner, source)
    assert calls[0][2]["stage"] == "dependencies"
    assert [stage for kind, stage, _ in calls if kind == "author"] == [
        "investigate-0",
        "design-0",
    ]
    assert result["usable"] == 1
