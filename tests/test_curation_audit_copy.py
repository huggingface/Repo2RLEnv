from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from repo2rlenv.curation import audit_copy
from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.audit_copy import (
    AuditIntegrityError,
    audit_subprocess_env,
    isolated_audit_copy,
)


def write(path: Path, content: bytes = b"inspection data\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "live" / "task"
    write(root / "instruction.md", "Read α and β.\n".encode())
    write(root / "tests" / "probe.py", b"raise RuntimeError('never execute this file')\n")
    # Path ordering is distinct from sorting complete relative-path strings.
    write(root / "a.py", b"one")
    write(root / "a" / "b", b"two")
    (root / "empty").mkdir()
    (root / "tests" / "probe.py").chmod(0o755)
    return root


def test_copy_is_verified_and_receipted_before_audit_then_checked_after(source, tmp_path):
    expected = digest_task(source)
    before = audit_copy._inventory(source)
    assert before["digest"] == expected
    with isolated_audit_copy(source, tmp_path / "audit", expected_digest=expected) as audit:
        receipt = json.loads(audit.receipt.read_text())
        assert receipt["status"] == "auditing"
        assert receipt["audit_started"] is True
        assert receipt["source_before"] == receipt["source_before_audit"] == before
        assert receipt["copy_before_audit"] == before
        assert receipt["expected_source_digest"] == audit.expected_digest == expected
        assert receipt["source_path"] == str(source.resolve())
        assert receipt["copy_path"] == str(audit.task)
        for entry in before["files"]:
            original, copied = source / entry["path"], audit.task / entry["path"]
            assert original.read_bytes() == copied.read_bytes()
            assert original.stat().st_ino != copied.stat().st_ino
            assert original.stat().st_mode == copied.stat().st_mode
        assert (audit.task / "empty").is_dir()
    receipt = json.loads(audit.receipt.read_text())
    assert receipt["status"] == "completed"
    assert receipt["source_after"] == receipt["copy_after"] == before
    assert receipt["finished_at"] >= receipt["created_at"]


def test_wrong_digest_refuses_before_destination_creation(source, tmp_path):
    destination = tmp_path / "audit"
    with pytest.raises(AuditIntegrityError, match="Unexpected source digest"):
        with isolated_audit_copy(source, destination, expected_digest="0" * 64):
            pytest.fail("Unverified source reached audit")
    assert not destination.exists()


def test_existing_bytecode_is_neither_excluded_from_digest_nor_removed(source, tmp_path):
    clean_digest = digest_task(source)
    contamination = write(source / "tests/__pycache__/probe.cpython-312.pyc", b"retained evidence")
    with pytest.raises(AuditIntegrityError, match="Unexpected source digest"):
        with isolated_audit_copy(source, tmp_path / "clean-audit", expected_digest=clean_digest):
            pytest.fail("Contamination was excluded from digest")
    observed_digest = digest_task(source)
    with isolated_audit_copy(
        source, tmp_path / "contaminated-audit", expected_digest=observed_digest
    ) as audit:
        assert (audit.task / contamination.relative_to(source)).read_bytes() == b"retained evidence"
    assert contamination.read_bytes() == b"retained evidence"
    assert digest_task(source) == observed_digest


@pytest.mark.parametrize("expected", ["", "abc", "A" * 64, "0" * 65])
def test_requires_exact_digest(source, tmp_path, expected):
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        with isolated_audit_copy(source, tmp_path / "audit", expected_digest=expected):
            pytest.fail("Invalid digest reached audit")


@pytest.mark.parametrize("relation", ["equal", "child", "ancestor", "aliased_child"])
def test_rejects_overlapping_destinations_without_mutation(source, tmp_path, relation):
    expected = digest_task(source)
    if relation == "equal":
        destination = source
    elif relation == "child":
        destination = source / "audit"
    elif relation == "ancestor":
        destination = source.parent
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(source, target_is_directory=True)
        destination = alias / "audit"
    with pytest.raises(ValueError, match="overlap"):
        with isolated_audit_copy(source, destination, expected_digest=expected):
            pytest.fail("Overlapping copy reached audit")
    assert digest_task(source) == expected


def test_never_overwrites_existing_audit(source, tmp_path):
    destination = tmp_path / "audit"
    marker = write(destination / "provenance.json", b"prior evidence")
    with pytest.raises(ValueError, match="existing evidence"):
        with isolated_audit_copy(source, destination, expected_digest=digest_task(source)):
            pytest.fail("Existing destination reached audit")
    assert marker.read_bytes() == b"prior evidence"


@pytest.mark.parametrize("root_kind", ["source", "destination"])
def test_rejects_symlink_roots(source, tmp_path, root_kind):
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    src, dest = (alias, tmp_path / "audit") if root_kind == "source" else (source, alias)
    with pytest.raises(ValueError, match="symlinks"):
        with isolated_audit_copy(src, dest, expected_digest=digest_task(source)):
            pytest.fail("Symlink root reached audit")


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo"])
def test_rejects_linked_and_nonregular_entries(source, tmp_path, kind):
    expected = digest_task(source)
    entry = source / "unsafe"
    if kind == "symlink":
        entry.symlink_to(source / "instruction.md")
    elif kind == "directory_symlink":
        entry.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(source / "instruction.md", entry)
    else:
        os.mkfifo(entry)
    with pytest.raises(AuditIntegrityError, match="Nonregular or linked"):
        with isolated_audit_copy(source, tmp_path / "audit", expected_digest=expected):
            pytest.fail("Unsafe entry reached audit")
    assert entry.lstat()
    assert not (tmp_path / "audit").exists()


@pytest.mark.parametrize(
    "bound,limit,message",
    [
        ("MAX_AUDIT_FILES", 2, "file"),
        ("MAX_AUDIT_ENTRIES", 3, "entry"),
        ("MAX_AUDIT_BYTES", 4, "byte"),
    ],
)
def test_inventory_is_bounded(source, tmp_path, monkeypatch, bound, limit, message):
    monkeypatch.setattr(audit_copy, bound, limit)
    with pytest.raises(AuditIntegrityError, match=f"{message} bound exceeded"):
        with isolated_audit_copy(source, tmp_path / "audit", expected_digest=digest_task(source)):
            pytest.fail("Oversized tree reached audit")
    assert not (tmp_path / "audit").exists()


@pytest.mark.parametrize("changed_tree", ["source", "copy"])
def test_added_bytecode_is_detected_retained_and_receipted(source, tmp_path, changed_tree):
    expected = digest_task(source)
    with pytest.raises(AuditIntegrityError, match=f"changed in {changed_tree}"):
        with isolated_audit_copy(source, tmp_path / "audit", expected_digest=expected) as audit:
            # Deliberate tempfile-only contamination to exercise post-audit detection.
            # No task module is imported or executed.
            root = source if changed_tree == "source" else audit.task
            contamination = write(
                root / "tests/__pycache__/probe.cpython-312.pyc", b"fake bytecode"
            )
    assert contamination.read_bytes() == b"fake bytecode"
    receipt = json.loads(audit.receipt.read_text())
    assert receipt["status"] == "integrity_error"
    assert receipt["changed_trees"] == [changed_tree]
    assert receipt[changed_tree + "_after"]["digest"] == digest_task(root) != expected
    unchanged_tree = "copy" if changed_tree == "source" else "source"
    assert receipt[unchanged_tree + "_after"] == receipt["source_before"]


def test_empty_directory_change_detected_even_when_content_digest_unchanged(source, tmp_path):
    expected = digest_task(source)
    with pytest.raises(AuditIntegrityError, match="changed in copy"):
        with isolated_audit_copy(source, tmp_path / "audit", expected_digest=expected) as audit:
            (audit.task / "unexpected-empty").mkdir()
    assert digest_task(audit.task) == expected
    assert (audit.task / "unexpected-empty").is_dir()
    assert "unexpected-empty" in json.loads(audit.receipt.read_text())["copy_after"]["directories"]


def test_pre_audit_recheck_rejects_change_during_copy(source, tmp_path, monkeypatch):
    expected = digest_task(source)
    copy_tree = audit_copy._copy_tree

    def concurrent_change(src, target, inventory):
        copy_tree(src, target, inventory)
        write(src / "concurrent-file", b"changed before audit")

    monkeypatch.setattr(audit_copy, "_copy_tree", concurrent_change)
    with pytest.raises(AuditIntegrityError, match="changed in source"):
        with isolated_audit_copy(source, tmp_path / "audit", expected_digest=expected):
            pytest.fail("Changed source reached audit")
    receipt = json.loads((tmp_path / "audit/provenance.json").read_text())
    assert receipt["audit_started"] is False
    assert "changed before audit" in receipt["error"]
    assert receipt["copy_before_audit"]["digest"] == expected
    assert (source / "concurrent-file").exists()


def test_audit_exception_preserves_post_checks_and_original_error(source, tmp_path):
    with pytest.raises(LookupError, match="inspection failed"):
        with isolated_audit_copy(
            source, tmp_path / "audit", expected_digest=digest_task(source)
        ) as audit:
            raise LookupError("inspection failed")
    receipt = json.loads(audit.receipt.read_text())
    assert receipt["status"] == "audit_error"
    assert receipt["error"] == "LookupError: inspection failed"
    assert receipt["source_after"] == receipt["copy_after"] == receipt["source_before"]


def test_unreadable_post_tree_is_failure_evidence_not_ignored(source, tmp_path):
    with pytest.raises(AuditIntegrityError, match="changed in copy"):
        with isolated_audit_copy(
            source, tmp_path / "audit", expected_digest=digest_task(source)
        ) as audit:
            (audit.task / "external-link").symlink_to(tmp_path)
    receipt = json.loads(audit.receipt.read_text())
    assert "Nonregular or linked" in receipt["copy_after"]["error"]
    assert (audit.task / "external-link").is_symlink()


def test_subprocess_environment_disables_bytecode_without_mutating_base(monkeypatch):
    base = {"PATH": "/safe/bin", "PYTHONPATH": "/live/task", "PYTHONDONTWRITEBYTECODE": "0"}
    expected = dict(base)
    env = audit_subprocess_env(base)
    assert env == {"PATH": "/safe/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1"}
    assert base == expected
    monkeypatch.setenv("PYTHONPATH", "/live/task")
    assert "PYTHONPATH" not in audit_subprocess_env()
    assert os.environ["PYTHONPATH"] == "/live/task"
