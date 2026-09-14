"""Campaign scheduling uses fake controllers; target code and providers never run."""

from concurrent.futures import Future
from pathlib import Path

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.loop.artifacts import digest
from repo2rlenv.tasksmith import batch
from repo2rlenv.tasksmith.models import Options


def candidate(number):
    return batch.Candidate(
        url=f"https://github.com/huggingface/accelerate/pull/{number}",
        options=Options(worker_reservation_usd="3", max_spend_usd="10"),
    )


class ImmediatePool:
    """Completed fake futures make admission behavior deterministic."""

    def __init__(self, **kwargs):
        self.maximum = kwargs["max_workers"]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, fn, *args):
        result = Future()
        try:
            result.set_result(fn(*args))
        except Exception as exc:
            result.set_exception(exc)
        return result


@pytest.fixture
def local_batch(tmp_path, monkeypatch):
    wheel = tmp_path / "owned.whl"
    wheel.write_text("frozen owned runtime")
    campaign = tmp_path / "campaign"
    BudgetLedger(campaign / "budget.sqlite3", limit_usd="1000")
    monkeypatch.setattr(batch, "check_runtime_wheel", digest)
    monkeypatch.setattr(batch, "_controller_identity", lambda: "controller-1")
    monkeypatch.setattr(batch, "ThreadPoolExecutor", ImmediatePool)
    monkeypatch.setattr(batch, "_freeze_controller", lambda *_: {})
    return tmp_path / "run", campaign, wheel


def success(url):
    return {
        "url": url,
        "status": "verified",
        "verified": {
            "url": url,
            "bundle_hash": "sha256:" + batch._key(url),
            "quality_result": str(Path("/") / batch._key(url) / "result.json"),
        },
    }


def test_target_counts_unique_prs_and_never_overdispatches(local_batch, monkeypatch):
    calls, events = [], []

    def run(config, item):
        calls.append(item["url"])
        return success(item["url"])

    monkeypatch.setattr(batch, "_supervise_candidate", run)
    plan = batch.BatchPlan(
        name="test", candidates=[candidate(i) for i in range(1, 5)], target_verified=2
    )
    report = batch.run_batch(plan, *local_batch, on_event=events.append)
    assert report["verified"] == 2
    assert report["new_verified"] == 2
    assert report["stop_reason"] == "target_reached"
    assert len(report["pending"]) == 2
    assert len(calls) == 2
    assert any("2 isolated PR workers" in event for event in events)


@pytest.mark.parametrize("changed", [False, True])
def test_approved_prior_inventory_is_checked_once_before_allocation(
    local_batch, monkeypatch, changed
):
    directory, campaign, wheel = local_batch
    expected = success(candidate(1).url)["verified"]
    fresh = dict(expected, bundle_hash="sha256:changed") if changed else expected
    checked, dispatched = [], []

    def verify(path):
        checked.append(path)
        return fresh

    def run(config, item):
        dispatched.append(item["url"])
        return success(item["url"])

    monkeypatch.setattr(batch, "verified_result", verify)
    monkeypatch.setattr(batch, "_supervise_candidate", run)
    plan = batch.BatchPlan(
        name="approved",
        candidates=[candidate(2)],
        target_verified=2,
        prior_verified=[directory.parent / "prior.json"],
    )
    if changed:
        with pytest.raises(ValueError, match="differs from the approved inventory"):
            batch.run_batch(plan, directory, campaign, wheel, expected_prior_verified=[expected])
        assert not (directory / "configuration.json").exists()
        assert not (directory / "runtime").exists()
        assert dispatched == []
    else:
        result = batch.run_batch(
            plan,
            directory,
            campaign,
            wheel,
            expected_prior_verified=[expected],
            on_event=lambda _: None,
        )
        assert result["verified"] == 2
        assert dispatched == [candidate(2).url]
    assert checked == plan.prior_verified


def test_gpu_cap_allows_cpu_work_without_overlapping_more_gpu_jobs(local_batch, monkeypatch):
    items = [candidate(i) for i in range(1, 7)]
    for item in items[:3]:
        item.options.gpus = 2
    admitted, waves = [], []

    def run(config, item):
        admitted.append(item["url"])
        return success(item["url"])

    def complete(active, **kwargs):
        waves.append(list(active.values()))
        return set(active), set()

    monkeypatch.setattr(batch, "_supervise_candidate", run)
    monkeypatch.setattr(batch, "wait", complete)
    plan = batch.BatchPlan(
        name="test", candidates=items, target_verified=6, max_parallel=4, max_gpu_parallel=2
    )
    result = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert waves[0] == [items[index].url for index in (0, 1, 3, 4)]
    assert all(sum(url in {item.url for item in items[:3]} for url in wave) <= 2 for wave in waves)
    assert len(admitted) == len(set(admitted)) == 6
    assert result["verified"] == 6 and result["stop_reason"] == "target_reached"


def test_drain_collects_active_children_and_never_admits_pending_work(local_batch, monkeypatch):
    output, _, _ = local_batch
    calls, waits = [], []

    def run(config, item):
        calls.append(item["url"])
        return {"url": item["url"], "status": "generated_unverified", "tasks": ["saved/task"]}

    def complete(active, **kwargs):
        waits.append(len(active))
        save_record(output / "drain-request.json", {"reason": "Use the next frozen runtime"})
        # Keep one child active so the next loop must drain it without replacing
        # the completed child with a third PR.
        first = next(iter(active))
        return {first}, set(active) - {first}

    monkeypatch.setattr(batch, "_supervise_candidate", run)
    monkeypatch.setattr(batch, "wait", complete)
    plan = batch.BatchPlan(name="test", candidates=[candidate(i) for i in range(1, 5)])
    result = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert len(calls) == 2 and waits == [2, 1]
    assert result["generated_unverified"] == 2
    assert result["stop_reason"] == "drained"
    assert len(result["pending"]) == 2


def test_existing_drain_request_prevents_any_dispatch(local_batch, monkeypatch):
    output, _, _ = local_batch
    save_record(output / "drain-request.json", {"reason": "Do not resume this batch"})
    monkeypatch.setattr(
        batch, "_supervise_candidate", lambda *_: pytest.fail("Unexpected dispatch")
    )
    plan = batch.BatchPlan(name="test", candidates=[candidate(1)])
    result = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert result["stop_reason"] == "drained" and result["pending"] == [candidate(1).url]


def test_generated_failures_retained_while_next_pr_fills_target(local_batch, monkeypatch):
    calls = []

    def run(config, item):
        calls.append(item["url"])
        if item["url"].endswith("/1"):
            return {
                "url": item["url"],
                "status": "generated_unverified",
                "tasks": ["retained/task"],
                "reason": "oracle failed",
            }
        return success(item["url"])

    monkeypatch.setattr(batch, "_supervise_candidate", run)
    plan = batch.BatchPlan(
        name="test", candidates=[candidate(i) for i in range(1, 5)], target_verified=2
    )
    report = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert report["new_verified"] == 2
    assert report["generated_unverified"] == 1
    assert report["candidates"][candidate(1).url]["tasks"] == ["retained/task"]
    assert len(calls) == 3


def test_batch_headroom_stops_dispatch_without_deleting_pending(local_batch, monkeypatch):
    monkeypatch.setattr(
        batch, "_supervise_candidate", lambda *_: pytest.fail("No dispatch authorized")
    )
    plan = batch.BatchPlan(name="test", candidates=[candidate(1)], max_spend_usd="2")
    report = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert report["stop_reason"] == "budget_headroom"
    assert report["pending"] == [candidate(1).url]
    assert report["budget"]["reserved_usd"] == "0"


def test_global_headroom_also_stops_dispatch(local_batch, monkeypatch):
    _, campaign, _ = local_batch
    ledger = BudgetLedger(campaign / "budget.sqlite3")
    ledger.reserve("previous-uncertain", "999", "existing operation")
    ledger.mark_uncertain("previous-uncertain", "provider outcome unknown")
    monkeypatch.setattr(
        batch, "_supervise_candidate", lambda *_: pytest.fail("No dispatch authorized")
    )
    plan = batch.BatchPlan(name="test", candidates=[candidate(1)])
    report = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert report["stop_reason"] == "budget_headroom"
    assert report["available_usd"] == "1.000000"
    assert ledger.status()["reserved_usd"] == "999.000000"


def test_controller_crash_blocks_new_dispatch_and_preserves_children(local_batch, monkeypatch):
    calls = []

    def run(config, item):
        calls.append(item["url"])
        raise RuntimeError("child crashed after provider dispatch")

    monkeypatch.setattr(batch, "_supervise_candidate", run)
    plan = batch.BatchPlan(name="test", candidates=[candidate(i) for i in range(1, 5)])
    report = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert report["stop_reason"] == "reconciliation_required"
    assert len(calls) == 2
    report = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert report["stop_reason"] == "reconciliation_required"
    assert len(calls) == 2


def test_unresolved_provider_cleanup_stops_new_work(local_batch, monkeypatch):
    calls = []

    def run(config, item):
        calls.append(item["url"])
        return {
            "url": item["url"],
            "status": "blocked",
            "cleanup_pending": ["workers/unknown.json"],
        }

    monkeypatch.setattr(batch, "_supervise_candidate", run)
    plan = batch.BatchPlan(name="test", candidates=[candidate(i) for i in range(1, 5)])
    report = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert report["stop_reason"] == "reconciliation_required"
    assert len(calls) == 2


def test_completed_blocked_candidates_are_not_retried_implicitly(local_batch, monkeypatch):
    calls = []

    def run(config, item):
        calls.append(item["url"])
        return {"url": item["url"], "status": "blocked", "reason": "unsupported source language"}

    monkeypatch.setattr(batch, "_supervise_candidate", run)
    plan = batch.BatchPlan(name="test", candidates=[candidate(1)])
    batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    report = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert calls == [candidate(1).url]
    assert report["stop_reason"] == "panel_exhausted"


def test_frozen_inputs_reject_silent_changes(local_batch, monkeypatch):
    plan = batch.BatchPlan(name="test", candidates=[candidate(1)], max_spend_usd="2")
    batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    changed = plan.model_copy(update={"max_spend_usd": "3"})
    with pytest.raises(ValueError, match="Frozen batch inputs changed"):
        batch.run_batch(changed, *local_batch)
    monkeypatch.setattr(batch, "_controller_identity", lambda: "controller-2")
    with pytest.raises(ValueError, match="Frozen batch inputs changed"):
        batch.run_batch(plan, *local_batch)


def test_frozen_wheel_rejects_tampering(local_batch):
    plan = batch.BatchPlan(name="test", candidates=[candidate(1)], max_spend_usd="2")
    batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    output, _, wheel = local_batch
    (output / "runtime" / wheel.name).write_text("changed")
    with pytest.raises(ValueError, match="Frozen runtime changed"):
        batch.run_batch(plan, *local_batch)


def test_finished_orphan_is_imported_without_redispatch(local_batch, monkeypatch):
    calls = []

    def run(config, item):
        calls.append(item["url"])
        raise RuntimeError("lost parent-child response")

    monkeypatch.setattr(batch, "_supervise_candidate", run)
    plan = batch.BatchPlan(name="test", candidates=[candidate(1)])
    batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    output, _, _ = local_batch
    save_record(
        output / "candidates" / batch._key(candidate(1).url) / "batch-result.json",
        {"url": candidate(1).url, "status": "generated_unverified", "tasks": ["retained/task"]},
    )
    report = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert report["generated_unverified"] == 1
    assert calls == [candidate(1).url]


def test_retained_inventory_includes_incomplete_task_and_cleanup_receipt(tmp_path):
    task = tmp_path / "candidates" / "a" / "quality" / "revisions" / "r1"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("incomplete but retained")
    save_record(tmp_path / "workers" / "worker.json", {"state": "creating", "operation_id": "test"})
    row = batch._outcome(tmp_path, candidate(1).url)
    assert row["status"] == "generated_unverified"
    assert row["tasks"] == [str(task)]
    assert row["cleanup_pending"] == [str(tmp_path / "workers" / "worker.json")]


def test_duplicate_and_nonfinite_plan_limits_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        batch.BatchPlan(
            name="test",
            candidates=[
                candidate(1),
                candidate(1).model_copy(update={"url": candidate(1).url + "/"}),
            ],
        )
    for value in ["NaN", "Infinity", "0", "-1"]:
        with pytest.raises(ValueError, match="finite and positive"):
            batch.BatchPlan(name="test", candidates=[candidate(1)], max_spend_usd=value)


def test_child_shares_scopes_and_reconciles_only_confirmed_worker(local_batch, monkeypatch):
    from repo2rlenv.quality.loop.client import RunBudget
    from repo2rlenv.tasksmith import runner as runner_module
    from repo2rlenv.tasksmith.author.budget import AuthorBudget

    output, campaign, wheel = local_batch
    selected = candidate(1)
    selected.source_record = {"url": selected.url, "head": "frozen"}
    plan = batch.BatchPlan(name="test", candidates=[selected], max_spend_usd="6")
    seen = {}

    class FakeTasksmith:
        def __init__(self, directory, campaign, options, wheel):
            self.directory = directory
            self.ledger = BudgetLedger(campaign / "budget.sqlite3")

        def run(self, panel, **kwargs):
            seen.update(kwargs)
            author = AuthorBudget(self.budget, self.directory / "author", "investigate")
            operation = author.reserve(1, "author")
            author.settle(operation, 0.1)
            quality = RunBudget(self.budget, self.prefix + "-q", "3")
            quality.reserve(quality.prefix + ":model:review", "1", "review")
            quality.settle(quality.prefix + ":model:review", "0.2", evidence="completed review")
            quality.reserve("native:" + quality.prefix + ":allocation", "1", "native")
            quality.settle(
                "native:" + quality.prefix + ":allocation", "0.3", evidence="terminated allocation"
            )
            worker_operation = "worker:modal:" + self.prefix + "-w0"
            self.budget.reserve(worker_operation, "3", "worker")
            save_record(
                self.directory / "workers" / "worker.json",
                {
                    "state": "terminated",
                    "operation_id": worker_operation,
                    "started_at": "2026-09-13T00:00:00+00:00",
                    "stopped_at": "2026-09-13T00:00:10+00:00",
                    "spec": {"cpus": 2, "memory_mb": 4096},
                },
            )
            save_record(self.directory / "report.json", {"candidates": [{"status": "generated"}]})
            task = self.directory / "retained-task"
            task.mkdir()
            (task / "task.toml").write_text("retained")

    monkeypatch.setattr(runner_module, "Tasksmith", FakeTasksmith)
    configuration = {
        "directory": str(output),
        "campaign": str(campaign),
        "wheel": str(wheel),
        "runtime": digest(wheel),
        "controller_sha256": "controller-1",
        "plan": plan.model_dump(mode="json"),
    }
    row = batch._run_candidate(configuration, selected.model_dump(mode="json"))
    assert row["status"] == "generated_unverified"
    assert not row["cleanup_pending"]
    assert seen["source_records"] == [selected.source_record]
    status = BudgetLedger(campaign / "budget.sqlite3").status()
    assert len(status["operations"]) == 4
    assert all(batch._prefix(output) in operation["id"] for operation in status["operations"])
    assert status["accounted_usd"] == "1.602111"
    assert status["reserved_usd"] == "0.000000"


def test_prior_verified_pr_is_skipped_not_counted_twice(local_batch, monkeypatch):
    prior_path = local_batch[0].parent / "prior-quality.json"
    prior_proof = success(candidate(1).url)["verified"]
    monkeypatch.setattr(batch, "verified_result", lambda _: prior_proof)
    calls = []

    def run(config, item):
        calls.append(item["url"])
        return success(item["url"])

    monkeypatch.setattr(batch, "_supervise_candidate", run)
    plan = batch.BatchPlan(
        name="test",
        candidates=[candidate(1), candidate(2)],
        prior_verified=[prior_path],
        target_verified=2,
    )
    report = batch.run_batch(plan, *local_batch, on_event=lambda _: None)
    assert report["prior_verified"] == 1
    assert report["new_verified"] == 1
    assert report["verified"] == 2
    assert calls == [candidate(2).url]


def frozen_test_controller(tmp_path, body):
    import zipfile

    wheel = tmp_path / "controller.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("repo2rlenv/__init__.py", "")
        archive.writestr("repo2rlenv/tasksmith/__init__.py", "")
        archive.writestr("repo2rlenv/tasksmith/batch.py", body)
        archive.writestr("repo2rlenv/tasksmith/late.py", "MARKER = 'frozen'\n")
    controller = tmp_path / "frozen"
    batch._freeze_controller(wheel, controller)
    return {
        "directory": str(tmp_path / "run"),
        "wheel": str(wheel),
        "controller_root": str(controller),
    }


def test_fresh_subprocess_imports_frozen_controller_and_preserves_private_logs(
    tmp_path, monkeypatch
):
    import os

    body = """
import json, os
from pathlib import Path

def _run_candidate(configuration, candidate):
    from .late import MARKER
    destination = Path(configuration['directory']) / 'candidates'
    import hashlib
    destination /= hashlib.sha256(candidate['url'].encode()).hexdigest()[:12]
    data = {'marker': MARKER, 'module': __file__, 'pid': os.getpid(),
            'sentinel': os.getenv('TASKSMITH_BATCH_TEST')}
    print('retained private output')
    (destination / 'batch-result.json').write_text(json.dumps(data))
"""
    configuration = frozen_test_controller(tmp_path, body)
    live = tmp_path / "live" / "repo2rlenv"
    live.mkdir(parents=True)
    (live / "__init__.py").write_text("raise RuntimeError('live package must not load')")
    monkeypatch.setenv("PYTHONPATH", str(live.parent))
    monkeypatch.setenv("TASKSMITH_BATCH_TEST", "inherited without mutation")
    before = dict(os.environ)
    result = batch._supervise_candidate(configuration, candidate(1).model_dump(mode="json"))
    assert result["marker"] == "frozen"
    assert result["pid"] != os.getpid()
    assert result["sentinel"] == "inherited without mutation"
    assert Path(result["module"]).is_relative_to(Path(configuration["controller_root"]))
    assert dict(os.environ) == before
    child = Path(configuration["directory"]) / "candidates" / batch._key(candidate(1).url)
    assert (child / "controller.stdout").read_text().strip() == "retained private output"
    assert (child / "controller.stdout").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="dispatch already exists"):
        batch._supervise_candidate(configuration, candidate(1).model_dump(mode="json"))


def test_subprocess_failure_keeps_diagnostics_and_never_exposes_them(tmp_path):
    configuration = frozen_test_controller(
        tmp_path,
        "def _run_candidate(*args):\n    raise RuntimeError('private diagnostic detail')\n",
    )
    with pytest.raises(RuntimeError, match="retained private logs") as error:
        batch._supervise_candidate(configuration, candidate(1).model_dump(mode="json"))
    assert "private diagnostic detail" not in str(error.value)
    child = Path(configuration["directory"]) / "candidates" / batch._key(candidate(1).url)
    assert "private diagnostic detail" in (child / "controller.stderr").read_text()
    assert (child / "dispatch.json").is_file()


def test_frozen_controller_rejects_changed_file_before_subprocess(tmp_path):
    configuration = frozen_test_controller(tmp_path, "def _run_candidate(*args):\n    pass\n")
    late = Path(configuration["controller_root"]) / "repo2rlenv/tasksmith/late.py"
    late.chmod(0o644)
    late.write_text("MARKER = 'changed'\n")
    with pytest.raises(ValueError, match="Frozen controller files changed"):
        batch._supervise_candidate(configuration, candidate(1).model_dump(mode="json"))
    child = Path(configuration["directory"]) / "candidates" / batch._key(candidate(1).url)
    assert not (child / "dispatch.json").exists()


def test_prepared_task_requires_source_and_forbids_generation_import(tmp_path):
    selected = candidate(1).model_dump(mode="json")
    with pytest.raises(ValueError, match="Prepared tasks require"):
        batch.Candidate.model_validate({**selected, "prepared_task": str(tmp_path)})
    with pytest.raises(ValueError, match="cannot use generation_run"):
        batch.Candidate.model_validate(
            {
                **selected,
                "source_record": {"url": selected["url"]},
                "prepared_task": str(tmp_path),
                "generation_run": str(tmp_path),
            }
        )


def test_supervisors_run_two_real_isolated_controllers_concurrently(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    body = """
import hashlib, json, os, time
from pathlib import Path

def _run_candidate(configuration, candidate):
    root = Path(configuration['directory'])
    (root / ('ready-' + str(os.getpid()))).write_text('ready')
    deadline = time.monotonic() + 5
    while len(list(root.glob('ready-*'))) < 2:
        if time.monotonic() > deadline:
            raise RuntimeError('controllers did not run concurrently')
        time.sleep(.01)
    path = root / 'candidates' / hashlib.sha256(candidate['url'].encode()).hexdigest()[:12]
    (path / 'batch-result.json').write_text(json.dumps({'pid': os.getpid()}))
"""
    configuration = frozen_test_controller(tmp_path, body)
    with ThreadPoolExecutor(max_workers=2) as supervisors:
        jobs = [
            supervisors.submit(
                batch._supervise_candidate, configuration, candidate(index).model_dump(mode="json")
            )
            for index in (1, 2)
        ]
        results = [job.result() for job in jobs]
    assert len({row["pid"] for row in results}) == 2
