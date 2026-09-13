"""Behavioral contracts for Tasksmith; no target code or cloud execution locally."""

from __future__ import annotations

import json
import time

import pytest
from pydantic import BaseModel

from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger
from repo2rlenv.pipelines.recipes.repository.export import export_repository_task
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile
from repo2rlenv.tasksmith.author.budget import AuthorBudget
from repo2rlenv.tasksmith.models import Panel, Profile


def test_component_spend_and_uncertainty_share_one_campaign(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10")
    pilot = RunBudget(ledger, "ts-one", "5")
    quality = RunBudget(pilot, "ts-one-q", "3")
    author = AuthorBudget(pilot, tmp_path / "charges", "investigate")
    operation = author.reserve(2, "author call")
    quality.reserve("trial:ts-one-q-oracle", "2", "quality call")
    with pytest.raises(BudgetExceeded):
        author.reserve(2, "must not dispatch")
    author.settle(operation, 0.5)
    assert pilot.totals() == {"accounted_usd": "0.5", "reserved_usd": "2"}
    ledger.mark_uncertain("trial:ts-one-q-oracle", "lost provider response")
    assert pilot.totals()["reserved_usd"] == "2"
    assert len(ledger.status()["operations"]) == 2


def test_selected_tests_and_release_notes_stay_private(tmp_path):
    base = tmp_path / "snapshot"
    for name, content in {
        "lib/core.py": "value = 1\n",
        "tests/test_core.py": "private test\n",
        "tests/other.py": "also private\n",
        "CHANGES.md": "the exact fix\n",
        "README.md": "package install text\n",
    }.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    options = PythonRepositoryProfile(
        source_paths=["lib"],
        test_paths=["tests"],
        test_selectors=["tests/test_core.py::test_value"],
        public_exclude=["CHANGES.md"],
    )
    task = export_repository_task(
        base=base,
        defective={"lib/core.py": b"value = 0\n"},
        reference={"lib/core.py": b"value = 1\n"},
        options=options,
        instruction="Correct the public behavior.",
        destination=tmp_path / "output",
        name="tasksmith-fixture",
        org="test",
        contrast={"FAIL_TO_PASS": ["tests.test_core::test_value"], "PASS_TO_PASS": []},
        metadata={"recipe": "tasksmith", "recipe_version": "1"},
    )
    assert not (task / "environment/source/tests").exists()
    assert not (task / "environment/source/CHANGES.md").exists()
    assert (task / "tests/source/tests/other.py").is_file()
    assert (task / "environment/source/README.md").is_file()
    assert (
        json.loads((task / "tests/contract.json").read_text())["test_paths"]
        == options.test_selectors
    )
    assert (task / "environment/source/lib/core.py").read_bytes() == b"value = 0\n"
    harbor = pytest.importorskip("harbor.models.task.task")
    config = harbor.Task(task).config
    assert config.verifier.environment_mode.value == "separate"
    assert config.agent.network_mode.value == "no-network"


def test_profile_requires_private_selectors_and_fixed_panel():
    with pytest.raises(ValueError, match="inside a private"):
        Profile(
            reasoning="Inspected dependency metadata.",
            resource="cpu",
            options=PythonRepositoryProfile(
                source_paths=["src"], test_paths=["tests"], test_selectors=["public/test_a.py"]
            ),
            dependency_inputs=["pyproject.toml"],
            upstream_test_rationale="Offline unit tests of the changed behavior.",
        )
    with pytest.raises(ValueError, match="unique"):
        Panel(name="fixed", prs=["one", "one"])


def test_remote_worker_refuses_controller_execution(monkeypatch, tmp_path):
    from repo2rlenv.tasksmith import worker

    monkeypatch.delenv("REPO2RLENV_REMOTE_WORKER", raising=False)
    monkeypatch.setattr("sys.argv", ["worker", str(tmp_path / "config"), str(tmp_path / "out")])
    with pytest.raises(RuntimeError, match="remote only"):
        worker.main()
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_author_revises_only_rejected_fields_and_reuses_completed_artifact(
    monkeypatch, tmp_path
):
    from repo2rlenv.tasksmith.author import artifact

    class Artifact(BaseModel):
        title: str
        count: int

    calls = 0

    async def agent(**kwargs):
        nonlocal calls
        calls += 1
        assert (
            "rejected"
            in (await kwargs["handlers"]["submit_artifact"](title="retain", count=0)).lower()
        )
        assert "committed" in await kwargs["handlers"]["revise_artifact"](patch={"count": 2})

    async def validate(value):
        if value.count < 1:
            raise ValueError("count must be positive")

    monkeypatch.setattr(artifact, "run_agent", agent)
    budget = RunBudget(BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10"), "ts-test", "5")
    kwargs = dict(
        schema=Artifact,
        stage="design",
        inputs={"source": "pinned"},
        system="test",
        prompt="test",
        root=tmp_path / "artifact",
        budget=AuthorBudget(budget, tmp_path / "charges", "design"),
        model="anthropic/claude-sonnet-4-6",
        runtime="pi",
        max_cost=1,
        max_turns=4,
        deadline=time.time() + 30,
        validate=validate,
    )
    result = await artifact.artifact_stage(**kwargs)
    assert result.title == "retain" and result.count == 2
    assert await artifact.artifact_stage(**kwargs) == result
    assert calls == 1
    with pytest.raises(ValueError, match="different inputs"):
        await artifact.artifact_stage(**{**kwargs, "inputs": {"source": "changed"}})


@pytest.mark.asyncio
async def test_incomplete_author_never_blindly_replays(monkeypatch, tmp_path):
    from repo2rlenv.tasksmith.author import artifact

    class Artifact(BaseModel):
        title: str

    calls = 0

    async def agent(**kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider disconnected")

    monkeypatch.setattr(artifact, "run_agent", agent)
    budget = RunBudget(BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10"), "ts-test", "5")
    kwargs = dict(
        schema=Artifact,
        stage="design",
        inputs={},
        system="test",
        prompt="test",
        root=tmp_path / "artifact",
        budget=AuthorBudget(budget, tmp_path / "charges", "design"),
        model="anthropic/claude-sonnet-4-6",
        runtime="pi",
        max_cost=1,
        max_turns=4,
        deadline=time.time() + 30,
    )
    with pytest.raises(RuntimeError, match="disconnected"):
        await artifact.artifact_stage(**kwargs)
    with pytest.raises(RuntimeError, match="reconciliation"):
        await artifact.artifact_stage(**kwargs)
    assert calls == 1


def test_graph_repairs_bootstrap_before_design_without_replacing_input(monkeypatch, tmp_path):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from types import SimpleNamespace

    from repo2rlenv.tasksmith import runner as module
    from repo2rlenv.tasksmith.models import Design, Options

    BudgetLedger(tmp_path / "campaign/budget.sqlite3", limit_usd="100")
    runner = module.Tasksmith(
        tmp_path / "run", tmp_path / "campaign", Options(), tmp_path / "wheel.whl"
    )
    runner.receipt, runner.worker, runner.python = (
        tmp_path / "worker.json",
        object(),
        "remote-python",
    )
    calls = []
    profile = Profile(
        reasoning="Inspected Python metadata and tests.",
        resource="cpu",
        options=PythonRepositoryProfile(
            source_paths=["lib"], test_paths=["tests"], test_selectors=["tests/test_lib.py"]
        ),
        dependency_inputs=["pyproject.toml"],
        upstream_test_rationale="Small offline regression suite.",
    )
    design = Design(
        instruction="Repair the public behavior while keeping all existing cases and errors compatible with the original library API.",
        requirements=[
            {
                "behavior": "Handle an empty input correctly",
                "verification": "Assert the resulting iterator is empty",
                "source_evidence": "tests/test_lib.py",
            }
        ],
        verifier_rationale="Existing regression tests cover the change.",
        wrong_solution_ideas=["Always return empty"],
        valid_alternative_ideas=["Use a different early guard"],
    )

    def remote(root, key, data):
        calls.append(key)
        if key == "inspect":
            return {"status": "completed", "value": {"checkout": "/remote/checkout"}}
        if key == "bootstrap-1":
            return {"status": "failed", "error": "README required by installer"}
        if key == "bootstrap-2":
            return {
                "status": "completed",
                "value": {"readiness": {"test": "passed"}, "dependency_cache": {"cache_hit": True}},
            }
        assert key == "construct-1"
        return {
            "status": "completed",
            "local": "/local/evidence",
            "value": {"task_relative": "task/generated"},
        }

    def author(root, stage, schema, inputs, checkout, **kwargs):
        calls.append(stage)
        if stage == "investigate-1":
            assert "README" in inputs["previous_failure"]["error"]
            assert inputs["previous_profile"] == profile.model_dump()
        return profile if schema is Profile else design

    class Loop:
        def __init__(self, *args, **kwargs):
            assert kwargs["protected_paths"] == ("solution", "environment/source")

        def run(self, task, **kwargs):
            assert str(task) == "/local/evidence/task/generated"
            return SimpleNamespace(
                status="usable",
                model_dump=lambda **kwargs: {
                    "status": "usable",
                    "task_path": "/local/evidence/task/generated",
                    "bundle_hash": "sha256:test",
                },
            )

    monkeypatch.setattr(module, "task_identity", lambda path: "sha256:test", raising=False)
    monkeypatch.setattr(runner, "remote", remote)
    monkeypatch.setattr(runner, "author", author)
    monkeypatch.setattr(module, "QualityLoop", Loop)
    monkeypatch.setattr(module, "RemoteTrials", lambda *args, **kwargs: SimpleNamespace())
    result = runner.candidate(
        {
            "id": "pinned",
            "url": "https://github.com/example/lib/pull/1",
            "source_files": ["lib/core.py"],
        }
    )
    assert result["status"] == "usable"
    assert calls == [
        "inspect",
        "investigate-0",
        "bootstrap-1",
        "investigate-1",
        "bootstrap-2",
        "design-0",
        "construct-1",
    ]
    assert runner.candidate(result["source"]) == result
    assert len(calls) == 7


def test_source_intake_rejects_a_moving_pr_and_malformed_url(monkeypatch):
    from repo2rlenv.tasksmith import source

    head_reads = 0

    def api(args):
        nonlocal head_reads
        if "/files?" in args[1]:
            return json.dumps(
                [
                    [
                        {
                            "filename": "lib/core.py",
                            "status": "modified",
                            "additions": 1,
                            "deletions": 1,
                        }
                    ]
                ]
            )
        head_reads += 1
        return json.dumps(
            {
                "merged_at": "2026-01-01",
                "base": {"sha": "a" * 40, "repo": {"private": False}},
                "head": {"sha": ("b" if head_reads == 1 else "c") * 40},
                "title": "fix",
                "body": "",
            }
        )

    monkeypatch.setattr(source, "_run_gh", api)
    monkeypatch.setattr(
        source,
        "fetch_pr_diff",
        lambda *args: (
            "diff --git a/lib/core.py b/lib/core.py\n--- a/lib/core.py\n+++ b/lib/core.py\n@@ -1 +1 @@\n-old\n+new\n"
        ),
    )
    with pytest.raises(ValueError, match="changed during intake"):
        source.resolve_pr("https://github.com/example/library/pull/1")
    with pytest.raises(ValueError, match="public GitHub"):
        source.resolve_pr("https://github.com/example/library/pull/1?redirect=other")


def test_supplemental_only_fail_to_pass_has_static_evidence(tmp_path):
    from repo2rlenv.quality.python_evidence import test_excerpts

    snippet = "def test_empty():\n    assert list(public_api([])) == []\n"
    result = test_excerpts(
        tmp_path,
        ["tests.tasksmith_behavior::test_empty"],
        additional_sources={"tests/tasksmith_behavior.py": snippet},
    )
    assert "test_empty" in result["tests/tasksmith_behavior.py"]
    assert not (tmp_path / "tests/tasksmith_behavior.py").exists()
