from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from repo2rlenv.tasksmith import build
from repo2rlenv.tasksmith.authoring import Construction, Design, Discovery
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.worker import canonical_digest, save_json


@pytest.fixture
def setup(tmp_path):
    candidate = tmp_path / "candidate"
    old = candidate / "revision-0"
    old.mkdir(parents=True)
    (old / "retained-evidence.json").write_text('{"status":"incomplete"}')
    root = candidate / "revision-0-attempt-1"
    source = {
        "id": "library-12",
        "repo": "example/library",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    discovery = Discovery(
        useful_outcome="Support reproducible public batching behavior.",
        behaviors=[
            {
                "id": "batching",
                "outcome": "Return independent deterministic batch assignments.",
                "source_evidence": ["library/batches.py"],
            }
        ],
        dependency_dockerfile="FROM python:3.12-slim@sha256:" + "c" * 64,
        dependency_inputs={"requirements.txt": "numpy==1.26.4"},
        readiness_commands=["python -c 'import library'"],
        upstream_tests="no_relevant_tests",
        resource_rationale="Tiny deterministic CPU-only public API fixtures.",
    )
    design = Design(
        proposals=[
            {
                "id": "batching",
                "task_request": "Make the public batching interface handle variable input lengths.",
                "included_behavior_ids": ["batching"],
                "verification_approach": "Compare tiny explicit sequences using an independent reference.",
                "independent_expectations": "Explicit independently enumerated item assignments per worker.",
                "plausible_wrong_solutions": ["Drop the final incomplete worker round."],
                "valid_alternatives": ["Equivalent index arithmetic."],
                "cost_and_uncertainty": "Small CPU fixtures.",
            }
        ],
        selected_id="batching",
        selection_rationale="This represents the complete bounded public behavior change.",
        fewer_proposals_reason="A single coherent behavior.",
    )
    files = {
        "instruction.md": b"Incomplete public request; must be corrected.\n",
        "solution/solve.sh": b"#!/bin/sh\nexit 0\n",
        "tests/test_contract.py": b"def test_fixture():\n    assert True\n",
        "tests/binary-fixture.bin": b"\x00\xff\x01",
    }
    rows = []
    for name, data in files.items():
        path = old / "captured" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rows.append(
            {
                "path": name,
                "local_path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    receipt = {
        "source_digest": canonical_digest(source),
        "target_root_name": root.name,
        "original_stage_root": str(old),
        "files": rows,
        "last_submission": {"cases": {"wrong_case_id": ["old input"]}},
        "design": design.model_dump(mode="json"),
        "reason": "Submit metadata used case labels instead of pytest names; public wording remains incomplete.",
        "bootstrap_discovery": discovery.model_dump(mode="json"),
    }
    receipt_path = candidate / "recovery-construction-attempt-1.json"
    save_json(receipt_path, receipt)
    return SimpleNamespace(
        candidate=candidate,
        old=old,
        root=root,
        source=source,
        discovery=discovery,
        design=design,
        files=files,
        receipt=receipt,
        receipt_path=receipt_path,
        config=TasksmithConfig(
            ledger_path=tmp_path / "budget.json", ledger_limit_usd=20, campaign_id="same-campaign"
        ),
        deadline=1234567890.0,
        cache=tmp_path / "same-bootstrap-cache",
    )


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_recovery_loads_exact_bytes_and_validated_design_without_mutating_source(setup):
    s = setup
    before = snapshot(s.old)
    result = build.load_recovery(s.root, s.source)
    assert dict(result["files"]) == s.files
    assert result["design"] == s.design
    assert result["receipt_digest"] == canonical_digest(s.receipt)
    assert snapshot(s.old) == before
    assert not s.root.exists()


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "target",
        "hash",
        "size",
        "negative_size",
        "bool_size",
        "oversize",
        "count",
        "duplicate",
        "absolute",
        "traversal",
        "backslash",
        "noncanonical",
        "relative_local",
        "design",
        "last_submission",
        "reason",
        "original_revision",
        "original_not_earlier",
    ],
)
def test_malformed_recovery_receipts_fail_before_any_restore(setup, change):
    s = setup
    row = s.receipt["files"][0]
    if change == "source":
        s.receipt["source_digest"] = "0" * 64
    elif change == "target":
        s.receipt["target_root_name"] = "revision-0-attempt-2"
    elif change == "hash":
        row["sha256"] = "0" * 64
    elif change == "size":
        row["size"] += 1
    elif change == "negative_size":
        row["size"] = -1
    elif change == "bool_size":
        row["size"] = True
    elif change == "oversize":
        row["size"] = 2_000_001
    elif change == "count":
        s.receipt["files"] = [row] * 151
    elif change == "duplicate":
        s.receipt["files"].append(dict(row))
    elif change == "absolute":
        row["path"] = "/tmp/escape"
    elif change == "traversal":
        row["path"] = "../escape"
    elif change == "backslash":
        row["path"] = "tests\\escape"
    elif change == "noncanonical":
        row["path"] = "tests//escape"
    elif change == "relative_local":
        row["local_path"] = "relative-file"
    elif change == "design":
        s.receipt["design"]["selected_id"] = "missing"
    elif change == "last_submission":
        s.receipt["last_submission"] = []
    elif change == "reason":
        s.receipt["reason"] = ""
    elif change == "original_revision":
        s.receipt["original_stage_root"] = str(s.old.with_name("revision-1"))
    elif change == "original_not_earlier":
        s.root.mkdir()
        s.receipt["original_stage_root"] = str(s.root)
    save_json(s.receipt_path, s.receipt)
    with pytest.raises(ValueError):
        build.load_recovery(s.root, s.source)


@pytest.mark.parametrize(
    "kind", ["file_link", "parent_link", "receipt_link", "target_link", "fifo", "directory"]
)
def test_nonregular_recovery_paths_are_rejected(setup, kind, tmp_path):
    s = setup
    path = s.old / "captured/instruction.md"
    if kind == "file_link":
        path.unlink()
        path.symlink_to(s.old / "retained-evidence.json")
    elif kind == "parent_link":
        link = tmp_path / "alias"
        link.symlink_to(s.old / "captured", target_is_directory=True)
        s.receipt["files"][0]["local_path"] = str(link / "instruction.md")
        save_json(s.receipt_path, s.receipt)
    elif kind == "receipt_link":
        actual = tmp_path / "actual-receipt.json"
        s.receipt_path.rename(actual)
        s.receipt_path.symlink_to(actual)
    elif kind == "target_link":
        s.root.symlink_to(s.old, target_is_directory=True)
    else:
        path.unlink()
        os.mkfifo(path) if kind == "fifo" else path.mkdir()
    with pytest.raises(ValueError):
        build.load_recovery(s.root, s.source)


def test_overlapping_file_paths_and_oversized_receipt_are_rejected(setup):
    s = setup
    s.receipt["files"][0]["path"] = "tests"
    save_json(s.receipt_path, s.receipt)
    with pytest.raises(ValueError, match="Overlapping"):
        build.load_recovery(s.root, s.source)
    s.receipt_path.write_bytes(b" " * 1_000_001)
    with pytest.raises(ValueError, match="oversized"):
        build.load_recovery(s.root, s.source)


def test_absent_recovery_is_optional_and_does_not_guess_other_attempts(setup):
    s = setup
    assert build.load_recovery(s.old, s.source) is None
    assert build.load_recovery(s.root.with_name("revision-0-attempt-2"), s.source) is None


@pytest.mark.asyncio
async def test_invalid_receipt_prevents_bootstrap_and_all_worker_calls(setup, monkeypatch):
    s = setup
    s.receipt["files"][0]["sha256"] = "0" * 64
    save_json(s.receipt_path, s.receipt)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid handoff reached a remote or model operation")

    monkeypatch.setattr(build, "bootstrap_ready", forbidden)
    monkeypatch.setattr(build, "artifact_stage", forbidden)
    with pytest.raises(ValueError, match="SHA256"):
        await build.construct(
            s.config,
            s.source,
            {"artifact": s.discovery.model_dump(), "source_receipt": {}},
            s.root,
            s.cache,
            s.deadline,
        )
    assert not s.root.exists()
    assert not s.config.ledger_path.exists()


@pytest.mark.asyncio
async def test_recovery_restores_after_readiness_skips_design_and_revalidates_payload(
    setup, monkeypatch
):
    s = setup
    before = snapshot(s.old)
    remote_files, events, workers, emissions = {}, [], [], []
    final_instruction = "Implement the complete public batching behavior.\n"

    async def write(path, data):
        assert events == ["prepared-and-ready"]
        remote_files[path] = data

    async def read(path):
        return remote_files[path]

    async def shell(*args, **kwargs):
        pytest.fail("Fixture must not execute author code")

    @asynccontextmanager
    async def ready(config, source, discovered, root, cache_root, deadline, budget):
        assert config is s.config and source == s.source and discovered == s.discovery
        assert root == s.root and cache_root == s.cache and deadline == s.deadline
        events.append("prepared-and-ready")
        yield (
            discovered,
            SimpleNamespace(deadline=deadline),
            {"passed": True, "cache_hit": True},
            SimpleNamespace(write=write, read=read, shell=shell),
        )

    async def artifact(**kwargs):
        workers.append(kwargs)
        assert kwargs["schema"] is Construction, "Retained Design must not be rerolled"
        assert remote_files == {"/output/task/" + k: v for k, v in s.files.items()}
        recovery = kwargs["inputs"]["construction_recovery"]
        assert recovery["receipt_digest"] == canonical_digest(s.receipt)
        assert recovery["last_submission"] == s.receipt["last_submission"]
        assert json.loads(kwargs["prompt"])["construction_recovery"] == recovery
        assert kwargs["deadline"] == s.deadline
        remote_files["/output/task/instruction.md"] = final_instruction.encode()
        value = SimpleNamespace(
            contract={},
            source_origin_probe="print('fixture')",
            model_dump=lambda **_: {"corrected": True},
        )
        await kwargs["validate"](value)
        return value

    def emit(path, source, recipe, **kwargs):
        emissions.append(kwargs)
        path.mkdir(parents=True)
        (path / "instruction.md").write_text(kwargs["instruction"])
        return {"task_digest": "e" * 64}

    monkeypatch.setattr(build, "bootstrap_ready", ready)
    monkeypatch.setattr(build, "artifact_stage", artifact)
    monkeypatch.setattr(build, "emit_task", emit)
    result = await build.construct(
        s.config,
        s.source,
        {"artifact": s.discovery.model_dump(), "source_receipt": {}},
        s.root,
        s.cache,
        s.deadline,
    )
    assert len(workers) == 1 and len(emissions) == 2
    assert result["public_instruction"] == final_instruction
    assert all(row["instruction"] == final_instruction for row in emissions)
    assert result["artifact"] == {"corrected": True}
    assert result["design"] == s.receipt["design"]
    assert result["recovery_receipt_digest"] == canonical_digest(s.receipt)
    assert snapshot(s.old) == before
    assert not s.config.ledger_path.exists()
    assert (
        await build.construct(
            s.config, s.source, {"artifact": s.discovery.model_dump()}, s.root, s.cache, s.deadline
        )
        == result
    )
    assert len(workers) == 1
    s.receipt["reason"] += " Changed after completion."
    save_json(s.receipt_path, s.receipt)
    with pytest.raises(ValueError, match="receipt changed"):
        await build.construct(
            s.config, s.source, {"artifact": s.discovery.model_dump()}, s.root, s.cache, s.deadline
        )
