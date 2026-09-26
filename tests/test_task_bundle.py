from __future__ import annotations

import copy
import os
import tomllib

import pytest
import tomli_w

from repo2rlenv.emitter import bundle as bundle_module
from repo2rlenv.emitter.bundle import (
    TaskBundle,
    TaskFile,
    bundle_hash,
    inspect_bundle,
    write_bundle,
)


@pytest.fixture
def bundle():
    return TaskBundle(
        name="fixture",
        org="tests",
        instruction="Restore the expected behavior.",
        metadata={"recipe": "fixture", "recipe_version": "1", "reward_kinds": ["test_execution"]},
        files={
            "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
            "environment/input.bin": TaskFile(b"\x00\xff"),
            "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
            "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 1\n", executable=True),
        },
    )


def test_roundtrip_and_verifier_tamper(bundle, tmp_path):
    path = write_bundle(bundle, tmp_path)
    assert inspect_bundle(path)["integrity_passed"]
    assert (path / "environment/input.bin").read_bytes() == b"\x00\xff"
    (path / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    assert not inspect_bundle(path)["integrity_passed"]


@pytest.mark.parametrize("change", ["image", "mode", "reward", "timeout"])
def test_execution_contract_changes_identity(bundle, change):
    revised = copy.deepcopy(bundle)
    if change == "image":
        revised.files["environment/Dockerfile"] = TaskFile.text("FROM python:3.13-slim\n")
    elif change == "mode":
        revised.files["environment/input.bin"] = TaskFile(b"\x00\xff", executable=True)
    elif change == "reward":
        revised.metadata["reward_kinds"] = ["different"]
    else:
        revised.verifier_timeout_sec += 1
    assert bundle_hash(bundle) != bundle_hash(revised)


@pytest.mark.parametrize(
    "name",
    ["../secret", "/tmp/secret", "tests/../secret", "tests//file", "tests/./file", "tests\\escape"],
)
def test_asset_traversal_rejected_before_emission(bundle, name, tmp_path):
    bundle.files[name] = TaskFile.text("invalid")
    with pytest.raises(ValueError, match="asset path"):
        write_bundle(bundle, tmp_path)
    assert not list(tmp_path.iterdir())


def test_never_overwrites_existing_task(bundle, tmp_path):
    path = write_bundle(bundle, tmp_path)
    with pytest.raises(FileExistsError):
        write_bundle(bundle, tmp_path)
    assert inspect_bundle(path)["integrity_passed"]


def test_resume_recovers_only_an_identical_completed_export(bundle, tmp_path):
    path = write_bundle(bundle, tmp_path)
    assert write_bundle(bundle, tmp_path, resume=True) == path
    bundle.instruction = "A different task must not reuse an existing export."
    with pytest.raises(ValueError, match="does not match"):
        write_bundle(bundle, tmp_path, resume=True)


def test_symlink_rejected(bundle, tmp_path):
    path = write_bundle(bundle, tmp_path)
    (path / "tests/link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        inspect_bundle(path)


@pytest.mark.skipif(os.name == "nt", reason="chmod cannot set a real POSIX mode on Windows")
def test_privileged_mode_rejected(bundle, tmp_path):
    """A privileged real mode on a tracked file is still caught: this fix
    only skips the POSIX cross-check on Windows (where it's meaningless by
    construction), it doesn't relax it on a platform where chmod is real."""
    path = write_bundle(bundle, tmp_path)
    (path / "tests/test.sh").chmod(0o4755)
    with pytest.raises(ValueError, match="mode"):
        inspect_bundle(path)


# ---------------------------------------------------------------------------
# tracked_files/executable_files: out-of-band executable-intent (see #130)
# ---------------------------------------------------------------------------


def _read_toml(path):
    return tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))


def test_write_bundle_records_tracked_and_executable_files(bundle, tmp_path):
    path = write_bundle(bundle, tmp_path)
    repo2env = _read_toml(path)["metadata"]["repo2env"]
    assert repo2env["tracked_files"] == [
        "environment/Dockerfile",
        "environment/input.bin",
        "solution/solve.sh",
        "tests/test.sh",
    ]
    assert repo2env["executable_files"] == ["solution/solve.sh", "tests/test.sh"]


def test_legacy_bundle_hash_is_unchanged():
    """Golden value captured from the pre-tracked_files algorithm (the exact
    fixture below, hashed by the code as it existed before this change) — a
    configuration with no tracked_files key must keep hashing exactly the
    same way forever, so already-published bundles never stop verifying."""
    legacy_bundle = TaskBundle(
        name="golden-task",
        org="golden-org",
        instruction="do the thing",
        files={
            "environment/Dockerfile": TaskFile.text("FROM alpine\n"),
            "solution/solve.sh": TaskFile.text("#!/bin/sh\necho solved\n", executable=True),
            "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
        },
        metadata={"recipe": "golden", "recipe_version": "1", "reward_kinds": ["test_execution"]},
    )
    # A legacy configuration/files pair is exactly what bundle_hash() built
    # before this change: no tracked_files key injected.
    legacy_configuration = legacy_bundle.configuration()
    legacy_files = {
        "instruction.md": TaskFile.text(legacy_bundle.instruction),
        **legacy_bundle.files,
    }
    assert "tracked_files" not in legacy_configuration["metadata"]["repo2env"]
    assert (
        bundle_module._identity(legacy_configuration, legacy_files)
        == "sha256:5b40a31e4f38c66f88da43a4f22e655b42427935dc14f35e67e120b282f001ed"
    )


@pytest.mark.skipif(os.name == "nt", reason="the legacy check requires a real POSIX chmod")
def test_legacy_bundle_on_disk_still_verifies(bundle, tmp_path):
    """A bundle built the way write_bundle() worked before this change (no
    tracked_files anywhere: hash computed and stamped by the untouched
    legacy branch of _identity()) must still round-trip through the
    untouched legacy branch of inspect_bundle() exactly as it always has.

    This is unchanged, pre-existing Windows behavior, not a regression: a
    legacy bundle's round-trip was never possible on Windows before this fix
    either (chmod can't set a real POSIX mode there), and this fix
    deliberately leaves that alone — tracked_files is what makes new bundles
    round-trip on Windows; legacy ones are explicitly out of scope."""
    configuration = bundle.configuration()
    files = {"instruction.md": TaskFile.text(bundle.instruction), **bundle.files}
    assert "tracked_files" not in configuration["metadata"]["repo2env"]
    configuration["metadata"]["repo2env"]["bundle_hash"] = bundle_module._identity(
        configuration, files
    )
    files["task.toml"] = TaskFile.text(tomli_w.dumps(configuration))
    task_dir = tmp_path / "legacy"
    task_dir.mkdir()
    for relative, asset in files.items():
        target = task_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(asset.content)
        target.chmod(asset.mode)
    result = inspect_bundle(task_dir)
    assert result["integrity_passed"]
    assert "tracked_files" not in _read_toml(task_dir)["metadata"]["repo2env"]


@pytest.mark.skipif(os.name == "nt", reason="the untracked-file check requires a real POSIX chmod")
def test_file_added_after_emission_keeps_legacy_leniency(bundle, tmp_path):
    """Other tooling (e.g. the quality loop, quality/loop/artifacts.py) adds
    files directly to an already-written bundle directory, then re-stamps
    bundle_hash by calling inspect_bundle() again — exactly that pattern. A
    file added this way isn't in tracked_files, so it must fall back to the
    pre-existing stat()-only check rather than being rejected as 'not
    declared executable' just because tracked_files never mentions it.

    Untracked additions were already never round-trippable on Windows before
    this fix (same chmod limitation as the legacy case above); this fix
    doesn't change that, it only fixes the bundle's own originally-declared
    files."""
    path = write_bundle(bundle, tmp_path)
    extra = path / "solution" / "quality-original-solve.sh"
    extra.write_text("#!/bin/sh\necho original\n", encoding="utf-8")
    extra.chmod(0o755)
    # Re-stamp, mirroring quality/loop/artifacts.py's own pattern.
    new_hash = inspect_bundle(path)["bundle_hash"]
    config = _read_toml(path)
    config["metadata"]["repo2env"]["bundle_hash"] = new_hash
    (path / "task.toml").write_bytes(tomli_w.dumps(config).encode())

    result = inspect_bundle(path)
    assert result["integrity_passed"]
    assert result["bundle_hash"] == new_hash
    # tracked_files is untouched by the addition — it still only covers what
    # write_bundle() originally emitted.
    assert (
        "quality-original-solve.sh" not in _read_toml(path)["metadata"]["repo2env"]["tracked_files"]
    )


@pytest.mark.skipif(os.name == "nt", reason="chmod is not meaningful on Windows")
def test_untracked_file_mode_tamper_still_detected(bundle, tmp_path):
    """The untracked-file leniency above must not become blindness to a real
    chmod tamper on that same file — its mode still rides along in the hash
    exactly as it did before this change, it just isn't cross-checked
    against a manifest entry that was never written for it."""
    path = write_bundle(bundle, tmp_path)
    extra = path / "solution" / "quality-original-solve.sh"
    extra.write_text("#!/bin/sh\necho original\n", encoding="utf-8")
    extra.chmod(0o755)
    new_hash = inspect_bundle(path)["bundle_hash"]
    config = _read_toml(path)
    config["metadata"]["repo2env"]["bundle_hash"] = new_hash
    (path / "task.toml").write_bytes(tomli_w.dumps(config).encode())
    assert inspect_bundle(path)["integrity_passed"]

    extra.chmod(0o644)
    assert not inspect_bundle(path)["integrity_passed"]


@pytest.mark.skipif(os.name == "nt", reason="chmod is not meaningful on Windows")
def test_new_bundle_posix_chmod_tamper_still_detected(bundle, tmp_path):
    """The real chmod-tamper defense stays exactly as strict for new bundles:
    flipping a file's real mode without updating the (hashed) manifest must
    still be caught, same as it always has been — as a raised error, not a
    quietly-returned False, matching the legacy check's own behavior."""
    path = write_bundle(bundle, tmp_path)
    assert inspect_bundle(path)["integrity_passed"]
    (path / "tests/test.sh").chmod(0o644)
    with pytest.raises(ValueError, match="mode"):
        inspect_bundle(path)


def test_new_bundle_integrity_holds_when_the_filesystem_cannot_represent_mode(bundle, tmp_path):
    """Windows-equivalent proof: force sys.platform to win32 (the code's own
    branch condition) and make every file's real mode wrong in exactly the
    way NTFS always is (no POSIX execute bit at all) — integrity must still
    pass, because executable-intent now comes from the hashed manifest, not
    a stat() call that platform can't answer correctly."""
    path = write_bundle(bundle, tmp_path)
    for item in path.rglob("*"):
        if item.is_file():
            os.chmod(item, 0o666)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bundle_module.sys, "platform", "win32")
        assert inspect_bundle(path)["integrity_passed"]


def test_new_bundle_content_tamper_still_caught_on_simulated_windows(bundle, tmp_path):
    """The mode-blindness above must not become blindness to real tampering:
    a changed file's bytes still break the hash even when the platform can't
    check modes at all."""
    path = write_bundle(bundle, tmp_path)
    (path / "tests/test.sh").write_text("#!/bin/sh\necho tampered\n")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bundle_module.sys, "platform", "win32")
        assert not inspect_bundle(path)["integrity_passed"]


def test_manifest_tamper_flips_integrity_without_touching_file_bytes(bundle, tmp_path):
    """executable_files is inside the hashed configuration, so flipping a
    declared executable-intent must still be caught by the hash itself even
    with every file's real bytes and mode left untouched. Checked with
    sys.platform forced to win32 so the POSIX chmod cross-check (which would
    otherwise also — and separately — catch this as a raised mode error) is
    out of the picture: this isolates the hash-based detection a real
    Windows host relies on exclusively, with no chmod check available at all."""
    path = write_bundle(bundle, tmp_path)
    config = _read_toml(path)
    config["metadata"]["repo2env"]["executable_files"] = ["solution/solve.sh"]
    (path / "task.toml").write_bytes(tomli_w.dumps(config).encode())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bundle_module.sys, "platform", "win32")
        assert not inspect_bundle(path)["integrity_passed"]


def test_invalid_executable_files_entry_rejected(bundle, tmp_path):
    path = write_bundle(bundle, tmp_path)
    config = _read_toml(path)
    config["metadata"]["repo2env"]["executable_files"] = ["../escape"]
    (path / "task.toml").write_bytes(tomli_w.dumps(config).encode())
    with pytest.raises(ValueError, match="asset path"):
        inspect_bundle(path)


def test_duplicate_executable_files_entries_rejected(bundle, tmp_path):
    path = write_bundle(bundle, tmp_path)
    config = _read_toml(path)
    config["metadata"]["repo2env"]["executable_files"] = [
        "solution/solve.sh",
        "solution/solve.sh",
    ]
    (path / "task.toml").write_bytes(tomli_w.dumps(config).encode())
    with pytest.raises(ValueError, match="duplicate"):
        inspect_bundle(path)


# ---------------------------------------------------------------------------
# Review regressions on #158: legacy resume, and file types checked before task.toml
# ---------------------------------------------------------------------------


def _write_legacy_export(bundle, destination):
    """Materialize a bundle exactly as ``main`` did before tracked_files existed:
    no tracked/executable fields, identity from the untouched legacy shape."""
    configuration = bundle.configuration()
    files = {"instruction.md": TaskFile.text(bundle.instruction), **bundle.files}
    configuration["metadata"]["repo2env"]["bundle_hash"] = bundle_module._identity(
        configuration, files
    )
    files["task.toml"] = TaskFile.text(tomli_w.dumps(configuration))
    task_dir = destination / bundle.name
    for relative, asset in files.items():
        target = task_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(asset.content)
        target.chmod(asset.mode)
    return task_dir


@pytest.mark.skipif(os.name == "nt", reason="a legacy export needs a real POSIX chmod")
def test_resume_accepts_an_unchanged_legacy_export(bundle, tmp_path):
    """An export written by ``main`` inspects fine, so resuming it must too — the
    comparison has to use the identity shape the export was written in."""
    task_dir = _write_legacy_export(bundle, tmp_path)
    assert inspect_bundle(task_dir)["integrity_passed"]

    assert write_bundle(bundle, tmp_path, resume=True) == task_dir
    # Resuming never rewrites the export, so it stays legacy-shaped.
    assert "tracked_files" not in _read_toml(task_dir)["metadata"]["repo2env"]


@pytest.mark.skipif(os.name == "nt", reason="a legacy export needs a real POSIX chmod")
def test_resume_still_rejects_a_changed_bundle_over_a_legacy_export(bundle, tmp_path):
    _write_legacy_export(bundle, tmp_path)
    bundle.instruction = "A different task must not reuse an existing export."
    with pytest.raises(ValueError, match="does not match"):
        write_bundle(bundle, tmp_path, resume=True)


def test_inspect_result_keys_are_unchanged(bundle, tmp_path):
    """Callers spread the result into their own records, so the legacy flag used
    by resume must not leak into it."""
    path = write_bundle(bundle, tmp_path)
    assert set(inspect_bundle(path)) == {"bundle_hash", "claimed_hash", "integrity_passed"}


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/user")


def test_symlinked_task_toml_is_rejected_without_reading_its_target(bundle, tmp_path):
    """The old order parsed task.toml first, so a link's target was read (and its
    parse error surfaced) before the link was rejected."""
    path = write_bundle(bundle, tmp_path)
    outside = tmp_path / "outside.toml"
    outside.write_text("this is [not valid toml", encoding="utf-8")
    (path / "task.toml").unlink()
    _symlink_or_skip(path / "task.toml", outside)

    with pytest.raises(ValueError, match="symlink") as excinfo:
        inspect_bundle(path)
    assert "task.toml" in str(excinfo.value)


def test_dangling_symlinked_task_toml_is_rejected_as_a_symlink(bundle, tmp_path):
    path = write_bundle(bundle, tmp_path)
    (path / "task.toml").unlink()
    _symlink_or_skip(path / "task.toml", tmp_path / "does-not-exist")

    with pytest.raises(ValueError, match="symlink"):
        inspect_bundle(path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_fifo_task_toml_is_rejected_instead_of_hanging(bundle, tmp_path):
    """Opening a FIFO blocks until a writer appears; the type check has to come
    first. Run in a daemon thread so a regression fails instead of hanging."""
    import threading

    path = write_bundle(bundle, tmp_path)
    (path / "task.toml").unlink()
    os.mkfifo(path / "task.toml")
    outcome: list[BaseException | None] = []

    def inspect() -> None:
        try:
            inspect_bundle(path)
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=inspect, daemon=True)
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), "inspect_bundle blocked opening a FIFO task.toml"
    assert isinstance(outcome[0], ValueError)
    assert "special file" in str(outcome[0])
