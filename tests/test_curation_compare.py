from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
from pathlib import Path

import pytest

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import CampaignConfig

comparison = importlib.import_module("repo2rlenv.curation.compare")
URL = "https://github.com/example/project/pull/1"
SOURCE = {
    "id": "example-project-1",
    "url": URL,
    "repo": "example/project",
    "base_sha": "base",
    "head_sha": "head",
    "body": "Original frozen PR description",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_comparison_bounds_active_pr_groups_with_three_matched_authors(
    harness, monkeypatch, concurrency
):
    out, config, calls, _, _ = harness
    config = config.model_copy(update={"concurrency": concurrency})
    urls = [URL, URL[:-1] + "2", URL[:-1] + "3"]
    ready, release = asyncio.Event(), asyncio.Event()
    started = []
    original = comparison.curate_one

    def resolve(url):
        return {**SOURCE, "url": url, "id": "example-project-" + url.rsplit("/", 1)[1]}

    async def curate(source, root, cfg, budget, seed_task=None):
        started.append((source["url"], cfg.author_runtime))
        if len(started) == concurrency * 3:
            ready.set()
        await release.wait()
        return await original(source, root, cfg, budget, seed_task)

    monkeypatch.setattr(comparison, "resolve_pr", resolve)
    monkeypatch.setattr(comparison, "curate_one", curate)
    running = asyncio.create_task(comparison.compare(urls, out, config))
    try:
        await asyncio.wait_for(ready.wait(), timeout=3)
        assert {url for url, _ in started} == set(urls[:concurrency])
        for url in urls[:concurrency]:
            assert {runtime for source, runtime in started if source == url} == set(
                comparison.RUNTIMES
            )
    finally:
        release.set()
    result = await running
    assert len(result["rows"]) == len(calls) == 9
    assert result["status"] == "complete"


def write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    out = tmp_path.resolve() / "comparison"
    config = CampaignConfig(target=1, budget_usd=50, max_candidate_usd=10)
    calls, resolutions = [], []
    controls = {"outcome": {}, "source_hash": "controller-v1"}

    def resolve(url):
        resolutions.append(url)
        return dict(SOURCE)

    async def curate(source, root, cfg, budget, seed_task=None):
        calls.append({"runtime": cfg.author_runtime, "source": dict(source), "seed": seed_task})
        write(root / "source.json", source)
        task = root / "revision-0/task"
        write(task / "contract.json", {"source": source["head_sha"]})
        key = budget.reserve(0.2, "mock-author")
        budget.settle(key, 0.1)
        outcome = controls["outcome"].get(cfg.author_runtime, "accepted")
        verdict = {
            "id": source["id"],
            "source": source["url"],
            "status": outcome,
            "score": 92 if outcome == "accepted" else None,
            "task_path": str(task),
            "task_digest": digest_task(task),
            "admission_version": comparison.ADMISSION_VERSION,
            "reasons": [],
        }
        write(root / "verdict.json", verdict)
        return verdict

    monkeypatch.setattr(comparison, "resolve_pr", resolve)
    monkeypatch.setattr(comparison, "curate_one", curate)
    monkeypatch.setattr(comparison, "runtime_path", lambda runtime: tmp_path / (runtime + ".mjs"))
    monkeypatch.setattr(
        comparison,
        "runtime_snapshot",
        lambda: {
            "source_hash": controls["source_hash"],
            "source_files": {"compare.py": "hash"},
            "versions": {"langgraph": "test"},
            "node_dependencies": {"opencode-ai": "test"},
        },
    )
    return out, config, calls, resolutions, controls


@pytest.mark.asyncio
async def test_comparison_passes_newest_attempt_checkpoint_to_each_author(harness):
    out, config, calls, _, _ = harness
    expected = {}
    for runtime in comparison.RUNTIMES:
        parent = out / "candidates" / runtime / SOURCE["id"]
        older = parent / "9/revision-0/task"
        write(older / "contract.json", {"source": "same copied task"})
        os.utime(older / "contract.json", ns=(100, 100))
        newer = parent / "10/revision-0/task"
        shutil.copytree(older, newer)
        write(parent / "10/prior-review/task/contract.json", {"archived": True})
        (parent / "11").mkdir()
        expected[runtime] = newer
    await comparison.compare([URL], out, config)
    assert len(calls) == 3
    assert {call["runtime"]: call["seed"] for call in calls} == expected


@pytest.mark.asyncio
async def test_resume_recovers_durable_completion_without_authoring_again(harness):
    out, config, calls, resolutions, controls = harness
    original = await comparison.compare([URL], out, config)
    row = original["rows"][0]
    original["rows"].remove(row)
    write(out / "comparison.json", original)
    # Simulate loss of the report update after completion, with release already done.
    controls["source_hash"] = "controller-v2"
    resumed = await comparison.compare([URL], out, config)
    assert len(calls) == 3
    assert resolutions == [URL]
    assert len(resumed["rows"]) == 3
    assert resumed["summary"][row["runtime"]]["accepted"] == 1
    assert [entry["source_hash"] for entry in resumed["runtime_history"]] == [
        "controller-v1",
        "controller-v2",
    ]
    assert {r["runtime_source_hash"] for r in resumed["rows"]} == {"controller-v1"}


@pytest.mark.asyncio
async def test_result_is_durable_before_release_and_release_can_resume(harness, monkeypatch):
    out, config, calls, _, _ = harness
    real_release = comparison.release_task
    failed = False

    def crash_after_release(source, destination):
        nonlocal failed
        result_path = source.parents[1] / "comparison-result.json"
        assert result_path.exists()
        real_release(source, destination)
        if not failed:
            failed = True
            raise RuntimeError("simulated interruption after atomic release")

    monkeypatch.setattr(comparison, "release_task", crash_after_release)
    with pytest.raises(ExceptionGroup, match="TaskGroup"):
        await comparison.compare([URL], out, config)
    started = len(calls)
    monkeypatch.setattr(comparison, "release_task", real_release)
    resumed = await comparison.compare([URL], out, config)
    assert len(calls) == started
    assert len(resumed["rows"]) == 3
    assert all(row["status"] == "accepted" for row in resumed["rows"])


@pytest.mark.asyncio
async def test_legacy_protocol_recovers_terminal_accepted_verdict_and_inputs(harness):
    out, config, calls, resolutions, _ = harness
    protocol = {
        "seeds": [URL],
        "config": config.model_dump(),
        "runtimes": list(comparison.RUNTIMES),
    }
    # An omitted default stays omitted in the original protocol on resume.
    del protocol["config"]["author_runtime"]
    write(out / "protocol.json", protocol)
    root = out / "candidates/opencode" / SOURCE["id"] / "old-attempt"
    write(root / "source.json", SOURCE)
    task = root / "revision-0/task"
    write(task / "contract.json", {"old": True})
    verdict = {
        "id": SOURCE["id"],
        "source": URL,
        "status": "accepted",
        "score": 93,
        "admission_version": comparison.ADMISSION_VERSION,
        "task_path": str(task),
        "task_digest": digest_task(task),
        "reasons": [],
    }
    write(root / "verdict.json", verdict)
    comparison.release_task(task, out / "tasks/opencode" / SOURCE["id"])
    resumed = await comparison.compare([URL], out, config)
    assert {call["runtime"] for call in calls} == {"pi", "langgraph"}
    assert not resolutions
    row = next(r for r in resumed["rows"] if r["runtime"] == "opencode")
    assert row["recovered_from"] == "accepted_verdict"
    assert row["duration_sec"] is None
    assert (root / "comparison-result.json").exists()
    assert json.loads((out / "protocol.json").read_text())["config"] == protocol["config"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["digest", "missing"])
async def test_completed_accepted_rows_fail_closed_when_release_or_admission_changes(
    harness, monkeypatch, damage
):
    out, config, calls, _, _ = harness
    manifest = await comparison.compare([URL], out, config)
    row = manifest["rows"][0]
    released = out / "tasks" / row["runtime"] / row["id"]
    if damage == "digest":
        (released / "contract.json").write_text("tampered")
    elif damage == "missing":
        shutil.rmtree(released)
    with pytest.raises(ValueError, match="Accepted"):
        await comparison.compare([URL], out, config)
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted_archive", [False, True])
async def test_changed_admission_protocol_archives_and_revalidates_once(
    harness, monkeypatch, interrupted_archive
):
    out, config, calls, _, _ = harness
    original = await comparison.compare([URL], out, config)
    old_version = comparison.ADMISSION_VERSION
    monkeypatch.setattr(comparison, "ADMISSION_VERSION", old_version + 1)
    original_records = {
        row["evidence_dir"]: (Path(row["evidence_dir"]) / "comparison-result.json").read_bytes()
        for row in original["rows"]
    }
    if interrupted_archive:
        move = comparison.shutil.move

        def interrupted(source, destination):
            move(source, destination)
            raise RuntimeError("crash after archive move")

        monkeypatch.setattr(comparison.shutil, "move", interrupted)
        with pytest.raises(RuntimeError, match="crash after archive move"):
            await comparison.compare([URL], out, config)
        monkeypatch.setattr(comparison.shutil, "move", move)
    resumed = await comparison.compare([URL], out, config)
    assert len(calls) == 6
    assert len(resumed["previous_attempts"]) == 3
    assert all(row["admission_version"] == old_version + 1 for row in resumed["rows"])
    for row in resumed["previous_attempts"]:
        assert row["status"] == "accepted"
        assert row["revalidation_required"] == old_version + 1
        assert digest_task(Path(row["archived_task"])) == row["task_digest"]
        assert (Path(row["evidence_dir"]) / "comparison-result.json").read_bytes() == (
            original_records[row["evidence_dir"]]
        )
    await comparison.compare([URL], out, config)
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_retry_failures_keeps_attempt_history_and_stable_cost_scope(harness):
    out, config, calls, resolutions, controls = harness
    controls["outcome"]["opencode"] = "infrastructure_failure"
    first = await comparison.compare([URL], out, config)
    failed = next(r for r in first["rows"] if r["runtime"] == "opencode")
    durable = Path(failed["evidence_dir"]) / "comparison-result.json"
    original_bytes = durable.read_bytes()
    controls["outcome"]["opencode"] = "execution_failure"
    second = await comparison.compare([URL], out, config, retry_failures=True)
    assert len(calls) == 4
    assert second["summary"]["opencode"]["execution_failures"] == 1
    assert second["summary"]["opencode"]["quality_rejected"] == 0
    assert len(second["previous_attempts"]) == 1
    assert durable.read_bytes() == original_bytes
    controls["outcome"]["opencode"] = "accepted"
    third = await comparison.compare([URL], out, config, retry_failures=True)
    assert len(calls) == 5
    assert len(third["previous_attempts"]) == 2
    row = next(r for r in third["rows"] if r["runtime"] == "opencode")
    assert row["charged_or_reserved_usd"] == pytest.approx(0.3)
    assert row["previous_attempts"][0] == failed["evidence_dir"]
    assert third["charged_or_reserved_usd"] == pytest.approx(0.5)
    report = (out / "comparison.md").read_text()
    assert "Previous attempts" in report
    assert "infrastructure_failure" in report
    assert "execution_failure" in report
    assert "not added to totals again" in report
    assert str(Path(failed["evidence_dir"]).relative_to(out)) in report
    assert Budget(out / "budget.json", 50, scope=f"opencode:{URL}").spent == pytest.approx(0.3)
    await comparison.compare([URL], out, config)
    assert len(calls) == 5
    assert resolutions == [URL]


@pytest.mark.asyncio
async def test_freeze_rejects_conflicting_legacy_source_inputs(harness):
    out, config, calls, _, _ = harness
    write(out / "candidates/pi" / SOURCE["id"] / "one/source.json", SOURCE)
    write(
        out / "candidates/opencode" / SOURCE["id"] / "two/source.json",
        {**SOURCE, "head_sha": "different-head"},
    )
    with pytest.raises(ValueError, match="different source metadata"):
        await comparison.compare([URL], out, config)
    assert not calls


@pytest.mark.asyncio
async def test_resume_rejects_protocol_changes_and_frozen_source_tampering(harness):
    out, config, calls, _, _ = harness
    await comparison.compare([URL], out, config)
    with pytest.raises(ValueError, match="same PRs, configuration"):
        await comparison.compare(
            [URL], out, config.model_copy(update={"author_turns": config.author_turns + 1})
        )
    protocol = json.loads((out / "protocol.json").read_text())
    protocol["sources"][URL]["body"] = "Changed after first author ran"
    write(out / "protocol.json", protocol)
    with pytest.raises(ValueError, match="Frozen comparison source changed"):
        await comparison.compare([URL], out, config)
    assert len(calls) == 3
