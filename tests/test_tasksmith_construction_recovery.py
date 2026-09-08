from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from repo2rlenv.curation.models import Contract
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


def test_legacy_recovery_cannot_collide_with_later_revision_attempt(setup):
    s = setup
    assert build.load_recovery(s.root, s.source) is not None
    assert build.load_recovery(s.candidate / "revision-1-attempt-1", s.source) is None
    assert s.receipt_path.exists()


def test_revision_scoped_recovery_restores_its_own_attempt(setup):
    s = setup
    old = s.candidate / "revision-1"
    old.mkdir()
    root = s.candidate / "revision-1-attempt-1"
    receipt = {**s.receipt, "target_root_name": root.name, "original_stage_root": str(old)}
    save_json(s.candidate / "recovery-construction-revision-1-attempt-1.json", receipt)
    result = build.load_recovery(root, s.source)
    assert result["receipt_digest"] == canonical_digest(receipt)
    assert dict(result["files"]) == s.files
    assert s.receipt_path.exists()


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


@pytest.fixture
def semantic(setup):
    pytest.importorskip("harbor")
    s = setup
    s.source["url"] = "https://github.com/example/library/pull/12"
    s.root = s.candidate / "revision-1"
    contract = Contract(
        title="Preserve batch assignment",
        rationale="Check complete public batching behavior.",
        source_paths=["src/library"],
        requirements=[
            {"id": "ordering", "behavior": "Preserve rank ordering", "tests": ["test_first"]},
            {
                "id": "tails",
                "behavior": "Preserve incomplete tails",
                "tests": ["test_second", "test_third"],
            },
        ],
        mutations=[
            {"name": "wrong-order", "rationale": "Incorrect ordering", "script": "false"},
            {"name": "wrong-tail", "rationale": "Incorrect tails", "script": "false"},
        ],
        equivalents=[{"name": "loop", "rationale": "Equivalent loop", "script": "true"}],
        min_tests=3,
    )
    tests = "from probe import run_probe\n" + "\n".join(
        f"def {name}():\n    assert run_probe('print(json.dumps({i}))') == {i}\n"
        for i, name in enumerate(("test_first", "test_second", "test_third"))
    )
    manifest = Construction(
        contract=contract,
        expected_values={
            name: {
                "provenance": "fixed_fixture",
                "explanation": "Independent fixed integer fixture",
                "evidence_ids": ["instruction"],
            }
            for name in ("test_first", "test_second", "test_third")
        },
        cases={
            name: ["Independent tiny batch assignment"]
            for name in ("test_first", "test_second", "test_third")
        },
        public_evidence={"ordering": ["instruction"], "tails": ["instruction"]},
        source_origin_probe="print(json.dumps({'behavior': [1, 2]}))",
        reference_explanation="Reference applies the complete public batching behavior change.",
    )
    task = s.old / "task"
    emitter = build.emit_task(
        task,
        s.source,
        s.discovery.dependency_dockerfile,
        execution_contract=contract,
        instruction="Implement deterministic rank assignment while preserving incomplete batches.",
        solution_script="#!/bin/sh\nset -eu\ntrue\n",
        protected_tests=tests,
        source_origin_probe=manifest.source_origin_probe,
    )
    s.previous = {
        "task_path": str(task),
        "task_revision": 0,
        "emitter": emitter,
        "artifact": manifest.model_dump(mode="json"),
        "design": s.design.model_dump(mode="json"),
        "bootstrap_discovery": s.discovery.model_dump(mode="json"),
    }
    s.parent_digest = emitter["task_digest"]
    return s


def test_semantic_capture_verifies_full_task_and_restores_only_authored_inputs(semantic):
    s = semantic
    before = snapshot(s.old)
    prior = build.load_prior_construction(s.root, s.source, s.previous, s.parent_digest)
    assert prior["context"]["artifact"] == s.previous["artifact"]
    assert prior["context"]["design"] == s.previous["design"]
    assert prior["context"]["task_digest"] == s.parent_digest
    assert set(dict(prior["files"])) == {
        "instruction.md",
        "contract.json",
        "solution/solve.sh",
        "tests/test_contract.py",
        "tests/source_origin_probe.py",
    }
    assert all(data == before["task/" + name] for name, data in prior["files"])
    assert snapshot(s.old) == before and not s.root.exists()


def test_semantic_capture_matches_canonical_digest_with_directory_prefixes(semantic):
    from repo2rlenv.curation.artifacts import digest_task

    s = semantic
    task = s.old / "task"
    (task / "tests/a").mkdir()
    (task / "tests/a/fixture.bin").write_bytes(b"\x00\x01")
    (task / "tests/a.py").write_text("fixture_data = 1\n")
    s.parent_digest = digest_task(task)
    s.previous["emitter"]["task_digest"] = s.parent_digest
    result = build.load_prior_construction(s.root, s.source, s.previous, s.parent_digest)
    assert result["context"]["task_digest"] == s.parent_digest


@pytest.mark.parametrize(
    "bad",
    [
        "changed_file",
        "symlink",
        "hardlink",
        "fifo",
        "wrong_parent",
        "source",
        "bootstrap",
        "contract",
        "probe",
        "skipped_revision",
        "oversize",
    ],
)
def test_semantic_capture_rejects_changed_or_misattributed_prior_work(semantic, bad):
    s = semantic
    task = s.old / "task"
    if bad == "changed_file":
        (task / "instruction.md").write_text("changed")
    elif bad == "symlink":
        (task / "linked").symlink_to(task / "instruction.md")
    elif bad == "hardlink":
        os.link(task / "instruction.md", task / "linked")
    elif bad == "fifo":
        os.mkfifo(task / "fifo")
    elif bad == "wrong_parent":
        s.parent_digest = "0" * 64
    elif bad == "source":
        s.previous["emitter"]["source"]["head_sha"] = "0" * 40
    elif bad == "bootstrap":
        s.previous["bootstrap_discovery"]["dependency_dockerfile"] += (
            "\nRUN python -m pip install --no-cache-dir numpy==1.26.4\n"
        )
    elif bad == "contract":
        s.previous["artifact"]["contract"]["title"] = "A different contract"
    elif bad == "probe":
        s.previous["artifact"]["source_origin_probe"] = "print(json.dumps('other'))"
    elif bad == "skipped_revision":
        s.root = s.root.with_name("revision-2")
    elif bad == "oversize":
        (task / "oversized.bin").write_bytes(b"x" * 2_000_001)
    with pytest.raises(ValueError):
        build.load_prior_construction(s.root, s.source, s.previous, s.parent_digest)
    assert not s.root.exists() and not s.config.ledger_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage, scope_change, expected_stages",
    [
        ("execution", False, ["construction"]),
        ("comprehension", False, ["design", "construction"]),
        ("execution", True, ["design", "construction"]),
    ],
)
async def test_semantic_repair_restores_context_and_reconsiders_scope_when_needed(
    semantic, monkeypatch, stage, scope_change, expected_stages
):
    s = semantic
    before = snapshot(s.old)
    files, calls = {}, []
    feedback = {
        "repairable": True,
        "status": "needs_repair",
        "stage": stage,
        "scope_change_required": scope_change,
        "reason": "Correct the specified public tail behavior.",
    }
    original_discovery = s.discovery.model_copy(
        update={"readiness_commands": ["incorrect old selector"]}
    )

    async def write(path, data):
        files[path] = data

    async def read(path):
        return files[path]

    async def shell(*args, **kwargs):
        pytest.fail("No target execution in this test")

    @asynccontextmanager
    async def ready(config, source, discovered, root, cache, deadline, budget):
        assert (
            config is s.config
            and source == s.source
            and deadline == s.deadline
            and cache == s.cache
        )
        assert discovered == s.discovery, "Corrected bootstrap discovery was lost"
        yield (
            discovered,
            SimpleNamespace(deadline=deadline),
            {"passed": True},
            SimpleNamespace(write=write, read=read, shell=shell),
        )

    async def artifact(**kwargs):
        calls.append(kwargs["stage"])
        context = kwargs["inputs"]["prior_revision"]
        assert context["task_digest"] == s.parent_digest
        assert context["artifact"] == s.previous["artifact"]
        assert context["design"] == s.previous["design"]
        assert kwargs["inputs"]["repair"] == feedback
        assert json.loads(kwargs["prompt"])["prior_revision"] == context
        assert files["/output/task/contract.json"] == before["task/contract.json"]
        assert kwargs["deadline"] == s.deadline
        if kwargs["schema"] is Design:
            changed = s.design.model_dump(mode="json")
            changed["proposals"][0]["task_request"] += " Clarify the repaired public scope."
            return Design.model_validate(changed)
        files["/output/task/instruction.md"] = (
            b"Implement deterministic rank assignments including every incomplete tail.\n"
        )
        value = Construction.model_validate(s.previous["artifact"])
        await kwargs["validate"](value)
        return value

    monkeypatch.setattr(build, "bootstrap_ready", ready)
    monkeypatch.setattr(build, "artifact_stage", artifact)
    result = await build.construct(
        s.config,
        s.source,
        {"artifact": original_discovery.model_dump(), "source_receipt": {}},
        s.root,
        s.cache,
        s.deadline,
        feedback,
        prior_construction=s.previous,
        parent_task_digest=s.parent_digest,
    )
    assert calls == expected_stages
    assert result["public_instruction"] == files["/output/task/instruction.md"].decode()
    assert result["prior_construction_digest"] == canonical_digest(s.previous)
    assert result["emitter"]["task_digest"] != s.parent_digest
    assert (result["design"] == s.previous["design"]) == (expected_stages == ["construction"])
    assert snapshot(s.old) == before and not s.config.ledger_path.exists()


@pytest.mark.asyncio
async def test_corrupt_semantic_parent_stops_before_bootstrap(semantic, monkeypatch):
    s = semantic
    (s.old / "task/instruction.md").write_text("changed")

    def forbidden(*args, **kwargs):
        pytest.fail("Changed prior task reached bootstrap")

    monkeypatch.setattr(build, "bootstrap_ready", forbidden)
    with pytest.raises(ValueError, match="digest changed"):
        await build.construct(
            s.config,
            s.source,
            {"artifact": s.discovery.model_dump()},
            s.root,
            s.cache,
            s.deadline,
            {"repairable": True, "stage": "execution"},
            prior_construction=s.previous,
            parent_task_digest=s.parent_digest,
        )
    assert not s.root.exists()


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
