from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import tomli_w

from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.models import Contract
from repo2rlenv.tasksmith import trials as t
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.emit import emit_task, verify_collection
from repo2rlenv.tasksmith.harbor_environments import TrackedEnvironment
from repo2rlenv.tasksmith.worker import save_json


@pytest.fixture
def task(tmp_path):
    contract = Contract(
        title="fixture",
        rationale="transport fixture",
        source_paths=["src/example"],
        requirements=[
            {"id": "a", "behavior": "a", "tests": ["test_a"]},
            {"id": "b", "behavior": "b", "tests": ["test_b", "test_c"]},
        ],
        mutations=[
            {"name": "a", "rationale": "a", "script": "false"},
            {"name": "b", "rationale": "b", "script": "false"},
        ],
        equivalents=[{"name": "equivalent", "rationale": "same result", "script": "true"}],
        min_tests=3,
    )
    path = tmp_path / "task"
    emit_task(
        path,
        {
            "id": "org-example-1",
            "repo": "org/example",
            "url": "https://github.com/org/example/pull/1",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
        },
        "FROM python:3.12-slim@sha256:" + "c" * 64 + "\nRUN python -m pip install pytest==8.4.2\n",
        execution_contract=contract,
        instruction="Observe the public fixture behavior.",
        solution_script="#!/bin/sh\ntrue\n",
        protected_tests="from probe import run_probe\n"
        + "\n".join(
            f'def test_{n}():\n    assert run_probe("print(json.dumps(1))") == 1\n' for n in "abc"
        ),
    )
    return path


@pytest.fixture
def config(tmp_path):
    return TasksmithConfig(
        ledger_path=tmp_path / "budget.json",
        ledger_limit_usd=20,
        campaign_id="fixture",
        campaign_limit_usd=20,
        candidate_limit_usd=15,
    )


def install_trial(monkeypatch, *, reward=1, broken=None):
    from harbor.trial.hooks import TrialEvent
    from harbor.trial.trial import Trial

    seen = []

    async def create(cfg):
        seen.append(cfg)
        folder = cfg.trials_dir / cfg.trial_name
        resource_dir = Path(cfg.environment.kwargs["receipt_root"])
        hook = None
        data = b"# trusted fixture data, never imported\n"
        inventory = {
            "src/example/__init__.py": {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        }
        environment = SimpleNamespace(
            exec=AsyncMock(
                return_value=SimpleNamespace(
                    return_code=1 if broken == "hook" else 0,
                    stdout=json.dumps(inventory),
                    stderr="bad inventory",
                )
            ),
            stop=AsyncMock(),
        )

        def add_hook(event, callback):
            nonlocal hook
            assert event == TrialEvent.AGENT_END
            hook = callback

        async def run():
            folder.mkdir(parents=True, exist_ok=True)
            for role in ("solver", "grader"):
                if broken == "missing-role" and role == "grader":
                    continue
                save_json(
                    resource_dir / (role + ".json"),
                    {
                        "provider": cfg.environment.type.value,
                        "role": role,
                        "resource_id": role + "-id",
                        "status": "uncertain"
                        if broken == "cleanup" and role == "grader"
                        else "stopped",
                        "cleanup_confirmed": not (broken == "cleanup" and role == "grader"),
                        "build_digest": t.BuildSpec.from_directory(
                            cfg.task.path / ("tests" if role == "grader" else "environment"),
                            role=role,
                        ).digest,
                        "deadline": cfg.environment.kwargs["absolute_deadline"],
                    },
                )
            await hook(None)
            target = folder / "artifacts/workspace/src/example/__init__.py"
            target.parent.mkdir(parents=True)
            target.write_bytes(data if broken != "host-transfer" else b"changed")
            manifest = [
                {
                    "source": "/workspace/src/example",
                    "destination": "artifacts/workspace/src/example",
                    "status": "ok",
                }
            ]
            if broken == "manifest":
                manifest = []
            save_json(folder / "artifacts/manifest.json", manifest)
            save_json(
                folder / "verifier/collection.json",
                {} if broken == "grader-transfer" else inventory,
            )
            save_json(
                folder / "verifier/details.json",
                {
                    "valid": broken != "incomplete",
                    "reward": reward,
                    "outcome": "passed" if reward == 1 else "submission_failure",
                    "collection_digest": verify_collection(inventory, inventory),
                    "origin": "/workspace/src/example/__init__.py"
                    if broken != "origin"
                    else "/site-packages/example/__init__.py",
                },
            )
            save_json(folder / "verifier/source-observation.json", {"observed": 7})
            if broken == "exception":
                exception = SimpleNamespace(
                    exception_type="AgentTimeoutError", exception_message="incomplete"
                )
            else:
                exception = None
            return SimpleNamespace(
                agent_result=SimpleNamespace(cost_usd=0.125),
                verifier_result=SimpleNamespace(rewards={"reward": reward}),
                exception_info=exception,
            )

        instance = SimpleNamespace(
            agent_environment=environment,
            add_hook=add_hook,
            run=run,
            _are_artifacts_collected=False,
        )
        seen.append(instance)
        return instance

    monkeypatch.setattr(Trial, "create", create)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["modal", "daytona"])
@pytest.mark.parametrize("reward", [0, 1])
async def test_healthy_binary_observations_require_exact_collection_and_both_cleanup_receipts(
    task, config, tmp_path, monkeypatch, provider, reward
):
    config = config.model_copy(update={"provider": provider})
    seen = install_trial(monkeypatch, reward=reward)
    budget = config.budget("pr1")
    outcome = await t.run_trial(
        task,
        config,
        budget,
        tmp_path / "trial",
        "baseline",
        script="true",
        deadline=time.time() + 300,
    )
    assert outcome.reward == reward and outcome.observed_reward == reward
    assert outcome.collection_verified and outcome.cleanup_confirmed
    assert (
        not outcome.materialization_verified
    )  # Requires a separately paired source-change witness.
    assert outcome.source_observation == {"observed": 7}
    assert outcome.error is None
    cfg, runtime = seen
    assert cfg.environment.import_path.endswith("Tracked" + provider.title() + "Environment")
    assert cfg.environment.kwargs["absolute_deadline"] <= time.time() + 300
    kwargs = cfg.agent.kwargs
    assert kwargs["budget_group"] == config.campaign_id and kwargs["budget_scope"] == budget.scope
    assert (
        kwargs["group_limit"] == budget.group_limit and kwargs["scope_limit"] == budget.scope_limit
    )
    call = runtime.agent_environment.exec.await_args.kwargs
    assert call["user"] == "root" and "/usr/local/bin/python -I -c" in call["command"]
    ledger = json.loads(budget.path.read_text())["entries"]
    assert len(ledger) == 1
    charge = ledger[outcome.reservation_id]
    assert charge["status"] == "estimated" and charge["charged_usd"] > 0
    assert charge["reserved_usd"] >= config.cloud_reservation_usd


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        "manifest",
        "host-transfer",
        "grader-transfer",
        "origin",
        "incomplete",
        "exception",
        "missing-role",
    ],
)
async def test_incomplete_or_inconsistent_evidence_never_becomes_behavioral_reward(
    task, config, tmp_path, monkeypatch, broken
):
    install_trial(monkeypatch, broken=broken)
    outcome = await t.run_trial(
        task,
        config,
        config.budget("pr1"),
        tmp_path / "trial",
        "solver",
        model=config.solver_models[0],
        deadline=time.time() + 300,
    )
    assert outcome.reward is None and outcome.error
    assert outcome.observed_reward == 1


@pytest.mark.asyncio
async def test_uncertain_cleanup_holds_shared_ledger_reservation(
    task, config, tmp_path, monkeypatch
):
    install_trial(monkeypatch, broken="cleanup")
    budget = config.budget("pr1")
    outcome = await t.run_trial(
        task, config, budget, tmp_path / "trial", "oracle", oracle=True, deadline=time.time() + 300
    )
    assert outcome.reward is None and not outcome.cleanup_confirmed
    assert outcome.cloud_cost_estimated_usd is None
    assert (
        json.loads(budget.path.read_text())["entries"][outcome.reservation_id]["status"]
        == "reserved"
    )


@pytest.mark.asyncio
async def test_hook_rejection_prevents_harbor_best_effort_recovery_export(
    task, config, tmp_path, monkeypatch
):
    seen = install_trial(monkeypatch, broken="hook")
    outcome = await t.run_trial(
        task,
        config,
        config.budget("pr1"),
        tmp_path / "trial",
        "baseline",
        deadline=time.time() + 300,
    )
    assert outcome.reward is None and "inventory" in outcome.error
    assert seen[1]._are_artifacts_collected


@pytest.mark.asyncio
async def test_no_repeated_dispatch_or_reservation_from_same_root(
    task, config, tmp_path, monkeypatch
):
    seen = install_trial(monkeypatch)
    budget = config.budget("pr1")
    root = tmp_path / "trial"
    await t.run_trial(task, config, budget, root, "baseline", deadline=time.time() + 300)
    prior = budget.path.read_bytes()
    with pytest.raises(FileExistsError):
        await t.run_trial(task, config, budget, root, "baseline", deadline=time.time() + 300)
    assert len(seen) == 2 and budget.path.read_bytes() == prior


@pytest.mark.asyncio
async def test_budget_and_profile_fail_before_trial_create(task, config, tmp_path, monkeypatch):
    seen = install_trial(monkeypatch)
    with pytest.raises(BudgetExceeded):
        await t.run_trial(
            task,
            config,
            Budget(tmp_path / "tiny.json", 0.01),
            tmp_path / "tiny",
            "baseline",
            deadline=time.time() + 300,
        )
    assert not seen
    raw = t.tomllib.loads((task / "task.toml").read_text())
    raw["environment"]["gpus"] = 1
    (task / "task.toml").write_text(tomli_w.dumps(raw))
    with pytest.raises(ValueError, match="profile"):
        await t.run_trial(
            task, config, config.budget(), tmp_path / "bad", "baseline", deadline=time.time() + 300
        )
    assert not seen and not (tmp_path / "bad/operation.json").exists()


@pytest.mark.asyncio
async def test_gold_derived_script_and_normal_solver_use_correct_agents(
    task, config, tmp_path, monkeypatch
):
    seen = install_trial(monkeypatch)
    await t.run_trial(
        task,
        config,
        config.budget(),
        tmp_path / "mutation",
        "mutation",
        script="patch-witness",
        oracle=True,
        deadline=time.time() + 300,
    )
    assert seen[0].agent.kwargs["oracle_dir"] == str((task / "solution").resolve())
    assert seen[0].agent.kwargs["script"] == "patch-witness"
    await t.run_trial(
        task,
        config,
        config.budget(),
        tmp_path / "oracle",
        "oracle",
        oracle=True,
        deadline=time.time() + 300,
    )
    assert seen[2].agent.name == "oracle"


class FakeBase:
    def __init__(self, environment_dir, session_id, **kwargs):
        self.environment_dir = environment_dir
        self.session_id = session_id
        self.task_env_config = SimpleNamespace(gpus=0, tpu=None)
        self._sandbox = None

    @staticmethod
    def type():
        return SimpleNamespace(value="modal")

    async def start(self, force_build):
        self._sandbox = SimpleNamespace(object_id="sb-fixture")
        self._capture(self._sandbox)

    async def stop(self, delete):
        self._sandbox = None

    async def exec(self, *args, **kwargs):
        return kwargs


class TrackedFake(TrackedEnvironment, FakeBase):
    async def _confirm_terminal(self):
        assert self._captured_resource.object_id == "sb-fixture"
        if getattr(self, "fail_confirmation", False):
            raise OSError("lookup unavailable")


@pytest.mark.asyncio
async def test_tracked_role_hashes_and_confirmation_survive_base_handle_clear(tmp_path):
    envdir = tmp_path / "tests"
    envdir.mkdir()
    (envdir / "Dockerfile").write_text("FROM fixture\n")
    env = TrackedFake(
        environment_dir=envdir,
        session_id="trial__verifier__trial",
        receipt_root=str(tmp_path / "receipts"),
        absolute_deadline=time.time() + 300,
    )
    await env.start()
    await env.stop()
    receipt = t._read_json(next((tmp_path / "receipts").glob("*.json")))
    assert receipt["role"] == "grader" and receipt["build_digest"]
    assert receipt["cleanup_confirmed"] and receipt["status"] == "stopped"
    assert env._sandbox is None and receipt["resource_id"] == "sb-fixture"
    with pytest.raises(RuntimeError, match="restart"):
        await env.start()


@pytest.mark.asyncio
async def test_tracked_swallowed_stop_error_cannot_claim_cleanup(tmp_path):
    envdir = tmp_path / "environment"
    envdir.mkdir()
    (envdir / "Dockerfile").write_text("FROM fixture\n")
    env = TrackedFake(
        environment_dir=envdir,
        session_id="trial__env",
        receipt_root=str(tmp_path / "receipts"),
        absolute_deadline=time.time() + 300,
    )
    await env.start()
    env.fail_confirmation = True
    with pytest.raises(OSError):
        await env.stop()
    receipt = t._read_json(next((tmp_path / "receipts").glob("*.json")))
    assert receipt["status"] == "uncertain" and not receipt["cleanup_confirmed"]


@pytest.mark.asyncio
async def test_real_harbor_create_accepts_local_task_and_tracked_modal_import(task, tmp_path):
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial

    # Construction only; run/start is never invoked and there is no cloud call.
    config = TrialConfig.model_validate(
        {
            "task": {"path": str(task)},
            "trials_dir": str(tmp_path / "real"),
            "trial_name": "construction-fixture",
            "environment": {
                "import_path": "repo2rlenv.tasksmith.harbor_environments:TrackedModalEnvironment",
                "kwargs": {
                    "receipt_root": str(tmp_path / "receipts"),
                    "absolute_deadline": time.time() + 300,
                },
            },
        }
    )
    trial = await Trial.create(config)
    assert isinstance(trial.agent_environment, TrackedEnvironment)
    assert not (tmp_path / "receipts").exists()
    trial._close_logger_handler()


@pytest.mark.asyncio
async def test_cancelled_trial_retains_incomplete_outcome_and_cleans_up(
    task, config, tmp_path, monkeypatch
):
    from harbor.trial.trial import Trial

    env = SimpleNamespace(stop=AsyncMock())

    async def cancelled():
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        Trial,
        "create",
        AsyncMock(
            return_value=SimpleNamespace(agent_environment=env, add_hook=Mock(), run=cancelled)
        ),
    )
    budget = config.budget("pr1")
    with pytest.raises(asyncio.CancelledError):
        await t.run_trial(
            task,
            config,
            budget,
            tmp_path / "cancelled",
            "solver",
            model=config.solver_models[0],
            deadline=time.time() + 300,
        )
    env.stop.assert_awaited_once_with(delete=True)
    retained = t._read_json(tmp_path / "cancelled/outcome.json")
    assert retained["reward"] is None and "CancelledError" in retained["error"]
    assert (
        json.loads(budget.path.read_text())["entries"][retained["reservation_id"]]["status"]
        == "reserved"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["Modal", "Daytona"])
async def test_tracked_create_does_not_use_harbor_outer_retry(monkeypatch, provider):
    from repo2rlenv.tasksmith import harbor_environments

    if provider == "Modal":
        from harbor.environments.modal import ModalEnvironment as Base
    else:
        from harbor.environments.daytona import DaytonaEnvironment as Base
    calls = []

    async def failed_once(self, *args, **kwargs):
        calls.append(True)
        raise OSError("lost create response")

    monkeypatch.setattr(Base._create_sandbox, "__wrapped__", failed_once)
    klass = getattr(harbor_environments, "Tracked" + provider + "Environment")
    env = klass.__new__(klass)
    env._remaining = lambda *args: 30
    env._receipt = Mock()
    env._capture = Mock()
    env._sandbox = None
    with pytest.raises(OSError, match="lost create"):
        await env._create_sandbox()
    assert calls == [True]
