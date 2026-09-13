"""Author-worker lifecycle at the native quality boundary; no provider calls."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.execution import lifecycle
from repo2rlenv.execution.base import WorkerSpec
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.tasksmith import matrix_runner
from repo2rlenv.tasksmith import runner as runner_module
from repo2rlenv.tasksmith.models import Options


@pytest.fixture
def runner(tmp_path):
    campaign = tmp_path / "campaign"
    BudgetLedger(campaign / "budget.sqlite3", limit_usd="100")
    options = Options(gpus=1, max_spend_usd="14", worker_reservation_usd="3")
    options.quality.max_spend_usd = "9"
    options.quality.solver_reservation_usd = "1.50"
    return runner_module.Tasksmith(tmp_path / "run", campaign, options, tmp_path / "runtime.whl")


def install_worker(runner, monkeypatch, *, provider="modal", terminate_error=None):
    name = runner.prefix + "-w0"
    operation = f"worker:{provider}:{name}"
    runner.budget.reserve(operation, "3", "Author worker")
    receipt = runner.directory / "workers" / f"{name}.json"
    save_record(
        receipt,
        {
            "state": "running",
            "operation_id": operation,
            "ledger": str(runner.ledger.path.resolve()),
            "worker_id": "fake-worker",
            "started_at": "2026-09-13T00:00:00+00:00",
            "spec": WorkerSpec(provider=provider, name=name, cpus=4, memory_mb=8192).model_dump(),
        },
    )
    calls = []

    class Worker:
        id = "fake-worker"

        def terminate(self):
            calls.append("terminate")
            if terminate_error:
                raise terminate_error

    worker = Worker()
    runner.worker, runner.receipt, runner.python = worker, receipt, "remote-python"

    def connect(selected_provider, worker_id):
        assert (selected_provider, worker_id) == (provider, worker.id)
        return worker

    monkeypatch.setattr(lifecycle, "connect_worker", connect)
    monkeypatch.setattr(lifecycle, "now", lambda: "2026-09-13T00:10:00+00:00")
    return receipt, operation, calls


@pytest.fixture
def task(tmp_path):
    return write_bundle(
        TaskBundle(
            name="fixture",
            org="tests",
            instruction="Restore the specified public behavior.",
            metadata={
                "recipe": "tasksmith",
                "recipe_version": "1",
                "reward_kinds": ["test_execution"],
            },
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 1\n", executable=True),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
            },
        ),
        tmp_path / "constructed",
    )


def fake_quality(monkeypatch, *, on_run=None):
    from repo2rlenv.quality.loop import native

    events = []

    class Native:
        def __init__(self, directory, budget, options):
            events.append("native_ready")
            self.budget = budget

    class Loop:
        def __init__(self, *args, **kwargs):
            self.budget = kwargs["budget"]

        def run(self, *args, **kwargs):
            events.append("quality_run")
            if on_run:
                on_run(self)
            return SimpleNamespace(
                status="reviewed", model_dump=lambda **kwargs: {"status": "reviewed"}
            )

    monkeypatch.setattr(runner_module, "QualityLoop", Loop)
    monkeypatch.setattr(native, "NativeModalTrials", Native)
    return events


def review(runner, task):
    return runner.review_candidate(
        runner.directory / "candidate",
        {"id": "abcdefgh1234"},
        {"local": str(task.parent), "value": {"task_relative": task.name}},
    )


def test_gpu_releases_confirmed_worker_before_native_dispatch(runner, task, monkeypatch):
    receipt, operation, calls = install_worker(runner, monkeypatch)
    events = fake_quality(monkeypatch, on_run=lambda loop: assert_released())

    def assert_released():
        assert runner.worker is runner.receipt is runner.python is None
        assert calls == ["terminate"]
        assert json.loads(receipt.read_text())["state"] == "terminated"
        saved = next(op for op in runner.ledger.status()["operations"] if op["id"] == operation)
        assert saved["status"] == "settled"

    review(runner, task)
    assert events == ["native_ready", "quality_run"]
    assert receipt.with_suffix(".cost.json").is_file()
    assert runner.budget.totals() == {"accounted_usd": "1.253248", "reserved_usd": "0"}


def test_release_restores_candidate_headroom_without_changing_caps(runner, monkeypatch):
    install_worker(runner, monkeypatch)
    author = runner.prefix + ":author"
    runner.budget.reserve(author, "3.50", "Completed author/build stages")
    runner.budget.settle(author, "3.50", evidence="saved prior usage")
    quality = RunBudget(runner.budget, runner.prefix + "-q", "9")
    controls = quality.prefix + ":controls"
    quality.reserve(controls, "3.20", "Completed controls")
    quality.settle(controls, "3.20", evidence="confirmed prior controls")
    quality.reserve(quality.prefix + ":model", "1.50", "Blind solver reservation")
    native = quality.prefix + ":native"
    with pytest.raises(BudgetExceeded):
        quality.reserve(native, "3", "Learner sandbox would exceed candidate14")
    runner.release_author_worker()
    quality.reserve(native, "3", "Learner fits after confirmed author-worker settlement")
    assert runner.budget.limit == 14 and quality.limit == 9
    assert runner.budget.totals() == {"accounted_usd": "7.953248", "reserved_usd": "4.5"}


def test_failed_termination_preserves_hold_and_prevents_native_dispatch(runner, task, monkeypatch):
    receipt, operation, calls = install_worker(
        runner, monkeypatch, terminate_error=RuntimeError("Provider termination not confirmed")
    )
    events = fake_quality(monkeypatch)
    with pytest.raises(RuntimeError, match="not confirmed"):
        review(runner, task)
    assert calls == ["terminate"] and events == []
    assert runner.receipt == receipt and runner.worker is not None
    assert json.loads(receipt.read_text())["state"] == "termination_uncertain"
    saved = next(op for op in runner.ledger.status()["operations"] if op["id"] == operation)
    assert saved["status"] == "uncertain" and saved["reserved_micros"] == 3000000
    assert not receipt.with_suffix(".cost.json").exists()


def test_settlement_failure_retains_proof_and_hold_but_clears_dead_handles(
    runner, task, monkeypatch
):
    receipt, operation, calls = install_worker(runner, monkeypatch)
    events = fake_quality(monkeypatch)

    def failed_settlement(*args, **kwargs):
        raise RuntimeError("Ledger unavailable")

    monkeypatch.setattr(runner.budget, "settle", failed_settlement)
    with pytest.raises(RuntimeError, match="Ledger unavailable"):
        review(runner, task)
    assert calls == ["terminate"] and events == []
    assert runner.worker is runner.python is None and runner.receipt == receipt
    assert json.loads(receipt.read_text())["state"] == "terminated"
    assert json.loads(receipt.with_suffix(".cost.json").read_text())["accounted_usd"] == "1.253248"
    saved = next(op for op in runner.ledger.status()["operations"] if op["id"] == operation)
    assert saved["status"] == "uncertain" and saved["actual_micros"] is None
    assert runner.budget.totals()["reserved_usd"] == "3"


@pytest.mark.parametrize("already_stopped", [False, True])
def test_resumed_gpu_quality_replays_author_receipt(runner, task, monkeypatch, already_stopped):
    receipt, _, calls = install_worker(runner, monkeypatch)
    if already_stopped:
        lifecycle.stop_worker(receipt, runner.budget)
    resumed = runner_module.Tasksmith(
        runner.directory, runner.ledger.path.parent, runner.options, runner.wheel
    )
    events = fake_quality(monkeypatch)
    review(resumed, task)
    assert calls == ["terminate"] and events == ["native_ready", "quality_run"]
    assert resumed.budget.totals() == {"accounted_usd": "1.253248", "reserved_usd": "0"}


def test_settlement_retry_preserves_receipt_and_never_reterminates(runner, task, monkeypatch):
    receipt, _, calls = install_worker(runner, monkeypatch)
    events = fake_quality(monkeypatch)

    def failed_settlement(*args, **kwargs):
        raise RuntimeError("Ledger unavailable")

    with monkeypatch.context() as fault:
        fault.setattr(runner.budget, "settle", failed_settlement)
        with pytest.raises(RuntimeError, match="Ledger unavailable"):
            review(runner, task)
    assert events == [] and runner.receipt == receipt
    proof = receipt.with_suffix(".cost.json").read_bytes()
    review(runner, task)
    assert calls == ["terminate"] and events == ["native_ready", "quality_run"]
    assert runner.worker is runner.receipt is runner.python is None
    assert receipt.with_suffix(".cost.json").read_bytes() == proof
    assert runner.budget.totals() == {"accounted_usd": "1.253248", "reserved_usd": "0"}


def test_cleanup_ledger_failure_clears_confirmed_dead_handles(runner, task, monkeypatch):
    receipt, _, calls = install_worker(runner, monkeypatch)
    events = fake_quality(monkeypatch)

    def failed_update(*args, **kwargs):
        raise RuntimeError("Ledger unavailable")

    monkeypatch.setattr(runner.budget, "mark_uncertain", failed_update)
    with pytest.raises(RuntimeError, match="Ledger unavailable"):
        review(runner, task)
    assert calls == ["terminate"] and events == []
    assert runner.worker is runner.python is None and runner.receipt == receipt
    assert json.loads(receipt.read_text())["state"] == "terminated"
    assert runner.budget.totals()["reserved_usd"] == "3"


def test_cpu_keeps_reusable_author_worker(runner, task, monkeypatch):
    runner.options.gpus = 0
    receipt, _, calls = install_worker(runner, monkeypatch)
    ready = []
    monkeypatch.setattr(runner, "ready_worker", lambda: ready.append(True))
    events = fake_quality(monkeypatch)
    review(runner, task)
    assert ready == [True] and calls == [] and events == ["quality_run"]
    assert runner.receipt == receipt and runner.worker is not None
    assert runner.budget.totals() == {"accounted_usd": "0", "reserved_usd": "3"}


def test_prepared_gpu_task_does_not_create_a_worker(runner, task, monkeypatch):
    def unexpected():
        raise AssertionError("Prepared GPU quality does not need an author worker")

    monkeypatch.setattr(runner, "ready_worker", unexpected)
    events = fake_quality(monkeypatch)
    review(runner, task)
    assert events == ["native_ready", "quality_run"]
    assert runner.ledger.status()["operations"] == []


def test_invalid_local_bundle_does_not_destroy_author_checkout(runner, task, monkeypatch):
    receipt, _, calls = install_worker(runner, monkeypatch)
    events = fake_quality(monkeypatch)
    (task / "instruction.md").write_text("Changed after construction.")
    with pytest.raises(ValueError, match="recorded bundle hash"):
        review(runner, task)
    assert calls == events == [] and runner.receipt == receipt


def test_nonmodal_termination_does_not_guess_modal_pricing(runner, monkeypatch):
    receipt, _, calls = install_worker(runner, monkeypatch, provider="daytona")
    runner.release_author_worker()
    assert calls == ["terminate"] and json.loads(receipt.read_text())["state"] == "terminated"
    assert not receipt.with_suffix(".cost.json").exists()
    assert runner.budget.totals() == {"accounted_usd": "0", "reserved_usd": "3"}


def test_batch_final_reconciliation_does_not_settle_released_worker_twice(tmp_path, monkeypatch):
    from repo2rlenv.tasksmith import batch

    RealTasksmith = runner_module.Tasksmith
    reconciliations = []
    reconcile = matrix_runner.reconcile_worker

    def counted_reconcile(receipt, budget):
        reconciliations.append(receipt)
        return reconcile(receipt, budget)

    class Child(RealTasksmith):
        def run(self, *args, **kwargs):
            install_worker(self, monkeypatch)
            self.release_author_worker()

    monkeypatch.setattr(matrix_runner, "reconcile_worker", counted_reconcile)
    monkeypatch.setattr(runner_module, "Tasksmith", Child)
    monkeypatch.setattr(batch, "_controller_identity", lambda: "fixed")
    monkeypatch.setattr(batch, "check_runtime_wheel", lambda path: "wheel-digest")
    monkeypatch.setattr(
        batch, "_outcome", lambda directory, url: {"status": "generated_unverified"}
    )
    campaign = tmp_path / "campaign"
    BudgetLedger(campaign / "budget.sqlite3", limit_usd="100")
    configuration = {
        "directory": str(tmp_path / "batch"),
        "campaign": str(campaign),
        "controller_sha256": "fixed",
        "wheel": str(tmp_path / "runtime.whl"),
        "runtime": "wheel-digest",
        "plan": {"max_spend_usd": "20"},
    }
    candidate = batch.Candidate(
        url="https://github.com/example/project/pull/1", options=Options(gpus=1, max_spend_usd="14")
    )
    result = batch._run_candidate(configuration, candidate.model_dump(mode="json"))
    assert len(reconciliations) == 1
    assert result["budget"] == {"accounted_usd": "1.253248", "reserved_usd": "0"}
