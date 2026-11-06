"""The separate verifier rebuilds the submission from the agent's tree.

These run the standalone script's functions against real Git repositories.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from repo2rlenv.pipelines import _grader_tools

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """A pristine image tree and an agent copy of it."""
    pristine = tmp_path / "pristine"
    (pristine / "pkg").mkdir(parents=True)
    (pristine / "pkg" / "core.py").write_text("def clamp(x):\n    return x\n")
    (pristine / "pkg" / "old.py").write_text("OLD = 1\n")
    (pristine / "tests").mkdir()
    (pristine / "tests" / "test_core.py").write_text("def test_x():\n    pass\n")
    (pristine / ".gitignore").write_text("node_modules/\n*.log\n")
    _git(pristine, "init", "-q")
    _git(pristine, "add", "-A")
    _git(pristine, "commit", "-qm", "base")
    # Environment the image ships: ignored deps and an untracked, unignored venv.
    (pristine / "node_modules" / "lib").mkdir(parents=True)
    (pristine / "node_modules" / "lib" / "index.js").write_text("module.exports = 1\n")
    (pristine / "env" / "bin").mkdir(parents=True)
    (pristine / "env" / "bin" / "python").write_text('#!/bin/sh\nexec python3 "$@"\n')
    agent = tmp_path / "agent"
    shutil.copytree(pristine, agent, symlinks=True)
    return pristine, agent


def _rebuild(pristine: Path, agent: Path, tmp_path: Path, **limits) -> tuple[str, dict, str]:
    status = _grader_tools.rebuild(
        agent=str(agent),
        pristine=str(pristine),
        patch_out=str(tmp_path / "submission.patch"),
        report_out=str(tmp_path / "submission.json"),
        max_bytes=limits.get("max_bytes", 1 << 20),
        max_files=limits.get("max_files", 100),
        timeout=60,
    )
    report = json.loads((tmp_path / "submission.json").read_text())
    patch = (tmp_path / "submission.patch").read_text(errors="replace") if status == "ok" else ""
    return status, report, patch


def test_source_changes_cross_and_environment_does_not(repos, tmp_path: Path):
    pristine, agent = repos
    (agent / "pkg" / "core.py").write_text("def clamp(x):\n    return max(0, min(1, x))\n")
    (agent / "pkg" / "helper.py").write_text("HELPER = 2\n")
    (agent / "pkg" / "old.py").unlink()
    # Environment tampering: shims in deps, an unignored image venv and a
    # .gitignore that tries to make node_modules count as source.
    (agent / "node_modules" / "lib" / "index.js").write_text("process.exit(0)\n")
    (agent / "node_modules" / ".bin").mkdir()
    (agent / "node_modules" / ".bin" / "jest").write_text("#!/bin/sh\nexit 0\n")
    (agent / "env" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    (agent / ".venv" / "bin").mkdir(parents=True)
    (agent / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    (agent / ".gitignore").write_text("*.log\n")
    (agent / "debug.log").write_text("noise\n")

    status, report, patch = _rebuild(pristine, agent, tmp_path)

    assert status == "ok"
    assert (agent / "pkg" / "core.py").read_text().startswith("def clamp(x):\n    return max")
    assert (agent / "pkg" / "helper.py").read_text() == "HELPER = 2\n"
    assert not (agent / "pkg" / "old.py").exists()
    assert (agent / "node_modules" / "lib" / "index.js").read_text() == "module.exports = 1\n"
    assert not (agent / "node_modules" / ".bin").exists()
    assert (agent / "env" / "bin" / "python").read_text().endswith('python3 "$@"\n')
    assert not (agent / ".venv").exists()
    assert not (agent / "debug.log").exists()
    changed = {line.split(" b/", 1)[1] for line in patch.splitlines() if line.startswith("diff ")}
    assert changed == {".gitignore", "pkg/core.py", "pkg/helper.py", "pkg/old.py"}
    assert report["changed_files"] == 4  # core.py, helper.py, old.py, .gitignore


def test_no_op_submission_restores_pristine_tree(repos, tmp_path: Path):
    pristine, agent = repos
    status, report, patch = _rebuild(pristine, agent, tmp_path)
    assert status == "ok" and patch == "" and report["changed_files"] == 0
    assert _git(agent, "status", "--porcelain", "--untracked-files=no") == ""


def test_symlink_out_of_tree_is_rejected(repos, tmp_path: Path):
    pristine, agent = repos
    os.symlink("/etc/passwd", agent / "pkg" / "link.py")
    status, report, _ = _rebuild(pristine, agent, tmp_path)
    assert status == "unsafe_symlink" and report["status"] == "unsafe_symlink"


def test_symlink_inside_tree_is_kept(repos, tmp_path: Path):
    pristine, agent = repos
    os.symlink("core.py", agent / "pkg" / "alias.py")
    status, _, _ = _rebuild(pristine, agent, tmp_path)
    assert status == "ok"
    assert os.readlink(agent / "pkg" / "alias.py") == "core.py"


def test_nested_repository_is_dropped(repos, tmp_path: Path):
    pristine, agent = repos
    nested = agent / "vendored"
    nested.mkdir()
    (nested / "x.py").write_text("X = 1\n")
    _git(nested, "init", "-q")
    _git(nested, "add", "-A")
    _git(nested, "commit", "-qm", "n")
    status, _, patch = _rebuild(pristine, agent, tmp_path)
    assert status == "ok"
    assert "vendored/x.py" in patch and ".git" not in patch.replace("diff --git", "")


def test_oversized_submission_is_rejected(repos, tmp_path: Path):
    pristine, agent = repos
    (agent / "pkg" / "big.py").write_text("x = 1\n" * 1000)
    status, _, _ = _rebuild(pristine, agent, tmp_path, max_bytes=100)
    assert status == "too_large"
    (agent / "pkg" / "big.py").unlink()
    for i in range(5):
        (agent / "pkg" / f"m{i}.py").write_text("\n")
    status, _, _ = _rebuild(pristine, agent, tmp_path, max_files=3)
    assert status == "too_large"


def test_symlink_chain_escaping_the_tree_is_rejected(repos, tmp_path: Path):
    # Each link passes a lexical check; together `x` points above the tree.
    pristine, agent = repos
    os.symlink(".", agent / "z")
    os.symlink("z/..", agent / "x")
    status, report, _ = _rebuild(pristine, agent, tmp_path)
    assert status == "unsafe_symlink" and report["status"] == "unsafe_symlink"


def test_working_tree_encoding_attribute_is_not_applied(repos, tmp_path: Path):
    # An in-tree .gitattributes asking git to re-encode files must neither
    # break the rebuild nor change the bytes that cross.
    pristine, agent = repos
    (agent / ".gitattributes").write_text("* working-tree-encoding=UTF-16\n")
    (agent / "pkg" / "bad.py").write_bytes(b"X = '\xff\xfe\xfa'\n")
    status, _, _ = _rebuild(pristine, agent, tmp_path)
    assert status == "ok"
    assert (agent / "pkg" / "bad.py").read_bytes() == b"X = '\xff\xfe\xfa'\n"


@pytest.mark.parametrize("name", [":(glob)x", ":(exclude)*", "*"])
def test_pathspec_magic_file_names_are_literal(repos, tmp_path: Path, name: str):
    pristine, agent = repos
    (agent / "pkg" / name).write_text("MAGIC = 1\n")
    status, _, _ = _rebuild(pristine, agent, tmp_path)
    assert status == "ok"
    assert (agent / "pkg" / name).read_text() == "MAGIC = 1\n"
    assert (agent / "pkg" / "core.py").exists()


def test_ignored_and_image_directories_are_never_read(repos, tmp_path: Path):
    # Unreadable files there would fail the rebuild if it opened them.
    pristine, agent = repos
    for directory in ("node_modules/huge", "env/lib", ".venv/lib"):
        (agent / directory).mkdir(parents=True)
        blocked = agent / directory / "blocked.py"
        blocked.write_text("x\n")
        blocked.chmod(0)
    try:
        status, _, patch = _rebuild(pristine, agent, tmp_path)
    finally:
        for directory in ("node_modules/huge", "env/lib", ".venv/lib"):
            target = agent / directory / "blocked.py"
            if target.exists():
                target.chmod(0o600)
    assert status == "ok" and patch == ""


def test_unreadable_agent_tree_is_a_graded_rejection(repos, tmp_path: Path):
    pristine, agent = repos
    blocked = agent / "pkg" / "secret.py"
    blocked.write_text("x\n")
    blocked.chmod(0)
    try:
        status, report, _ = _rebuild(pristine, agent, tmp_path)
    finally:
        blocked.chmod(0o600)
    if os.geteuid() == 0:
        pytest.skip("root reads unreadable files")
    assert status == "invalid" and "error" in report


def test_missing_pristine_copy_raises(tmp_path: Path):
    (tmp_path / "agent").mkdir()
    with pytest.raises(_grader_tools.PreReadFailed):
        _rebuild(tmp_path / "missing", tmp_path / "agent", tmp_path)


def test_exit_status_marks_only_grader_failures_as_ungraded(tmp_path: Path):
    # Before the agent's tree is read: the dedicated status test.sh leaves ungraded.
    (tmp_path / "agent").mkdir()
    code = _grader_tools.main(
        [
            "rebuild",
            "--agent",
            str(tmp_path / "agent"),
            "--pristine",
            str(tmp_path / "missing"),
            "--patch-out",
            str(tmp_path / "p"),
            "--report-out",
            str(tmp_path / "r"),
        ]
    )
    assert code == _grader_tools.EXIT_PRE_READ


def test_huge_files_are_judged_by_size_without_being_read(repos, tmp_path: Path, monkeypatch):
    # A file bigger than the verifier's memory must not be read whole (an
    # out-of-memory kill would otherwise turn into an ungraded run).
    pristine, agent = repos
    with open(agent / "huge.dat", "wb") as handle:  # untracked, sparse: a stray binary
        handle.truncate(4 << 30)
    reads = []
    real_open = _grader_tools._open_regular

    def spy(path):
        reads.append(path)
        handle = real_open(path)
        if handle is not None and path.endswith("huge.dat"):
            real_read = handle.read

            def bounded(size=-1):
                assert 0 <= size <= _grader_tools._BINARY_PROBE, "read past the binary probe"
                return real_read(size)

            handle.read = bounded
        return handle

    monkeypatch.setattr(_grader_tools, "_open_regular", spy)
    status, report, patch = _rebuild(pristine, agent, tmp_path)
    assert status == "ok" and patch == ""
    assert "huge.dat" in report["skipped"]
    assert report["skipped_count"] == len(report["skipped"])

    # A tracked file grown past the cap: too_large from its size alone.
    with open(agent / "pkg" / "core.py", "wb") as handle:
        handle.truncate(4 << 30)
    reads.clear()
    status, _, _ = _rebuild(pristine, agent, tmp_path)
    assert status == "too_large"
    assert not any(path.endswith("core.py") for path in reads)


def test_stray_binaries_are_skipped_and_new_source_crosses(repos, tmp_path: Path):
    pristine, agent = repos
    (agent / "pkg" / "core.py").write_text("def clamp(x):\n    return max(0, min(1, x))\n")
    (agent / "tool.bin").write_bytes(b"\x7fELF" + b"\0" * (20 << 20))  # a `go build` binary
    # New source in a directory named like build output, and a large generated table.
    (agent / "src" / "build").mkdir(parents=True)
    (agent / "src" / "build" / "x.py").write_text("VALUE = 1\n")
    (agent / "pkg" / "table.py").write_text("TABLE = [\n" + "    1,\n" * (2 << 18) + "]\n")
    (agent / "node_modules" / "dep").mkdir(parents=True)  # a dependency dir: never read
    (agent / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n")
    status, report, patch = _rebuild(pristine, agent, tmp_path, max_bytes=16 << 20)
    assert status == "ok"
    assert (agent / "pkg" / "table.py").stat().st_size > 2 << 20
    changed = {line.split(" b/", 1)[1] for line in patch.splitlines() if line.startswith("diff ")}
    assert changed == {"pkg/core.py", "src/build/x.py", "pkg/table.py"}
    # The fixture's image ships `env/` untracked; it and node_modules are never read.
    assert {"node_modules/", "env/", "tool.bin"} <= set(report["skipped"])
    assert not any(path.startswith(("src/", "pkg/")) for path in report["skipped"])


def test_untracked_text_counts_towards_the_caps(repos, tmp_path: Path):
    pristine, agent = repos
    (agent / "notes.txt").write_text("x" * (17 << 20))  # text over the 16 MiB cap
    status, _, _ = _rebuild(pristine, agent, tmp_path, max_bytes=16 << 20)
    assert status == "too_large"


def _crlf_repos(tmp_path: Path, attributes: str, body: bytes) -> tuple[Path, Path]:
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    (pristine / ".gitattributes").write_text(attributes)
    (pristine / "make.bat").write_bytes(body)
    _git(pristine, "init", "-q")
    _git(pristine, "add", "-A")
    _git(pristine, "commit", "-qm", "base")
    # The image's checkout: rewrite the file the way git checks it out.
    (pristine / "make.bat").unlink()
    _git(pristine, "checkout", "--", "make.bat")
    agent = tmp_path / "agent"
    shutil.copytree(pristine, agent, symlinks=True)
    return pristine, agent


@pytest.mark.parametrize(
    ("attributes", "body"),
    [
        ("*.bat text eol=crlf\n", b"@echo off\nset A=1\nexit /b 0\n"),
        ("*.bat ident\n", b"rem $Id$\nset A=1\nexit /b 0\n"),
    ],
)
def test_checkout_that_differs_from_blobs_still_applies(tmp_path: Path, attributes, body):
    # The base side of the diff is the image's checked-out file, so an honest
    # one-line fix applies even where git's stored blob differs from it.
    pristine, agent = _crlf_repos(tmp_path, attributes, body)
    checked_out = (pristine / "make.bat").read_bytes()
    assert checked_out != body  # git did convert it on checkout
    fixed = checked_out.replace(b"set A=1", b"set A=2")
    (agent / "make.bat").write_bytes(fixed)
    status, report, patch = _rebuild(pristine, agent, tmp_path)
    assert status == "ok", report
    assert (agent / "make.bat").read_bytes() == fixed
    # An unchanged file in such a repository is not a change at all.
    shutil.rmtree(agent)
    shutil.copytree(pristine, agent, symlinks=True)
    status, report, patch = _rebuild(pristine, agent, tmp_path)
    assert status == "ok" and patch == "" and report["changed_files"] == 0


def test_same_size_edit_right_after_the_copy_is_seen(repos, tmp_path: Path):
    # Validation copies the base tree and applies the gold patch within the same
    # second; a same-size edit must still count as a change.
    pristine, agent = repos
    (agent / "pkg" / "old.py").write_text("OLD = 2\n")
    status, _, patch = _rebuild(pristine, agent, tmp_path)
    assert status == "ok" and "pkg/old.py" in patch


def test_agent_tree_is_moved_aside_not_deleted(repos, tmp_path: Path):
    pristine, agent = repos
    (agent / "pkg" / "core.py").write_text("FIXED = 1\n")
    status, report, _ = _rebuild(pristine, agent, tmp_path)
    assert status == "ok"
    aside = Path(report["agent_tree_aside"])
    assert (aside / "pkg" / "core.py").read_text() == "FIXED = 1\n"
    assert (aside / "node_modules" / "lib" / "index.js").exists()
    assert report["copy_seconds"] >= 0


def test_failed_copy_of_the_image_repository_is_a_grader_failure(repos, tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root copies unreadable files")
    pristine, agent = repos
    blocked = pristine / "pkg" / "unreadable.py"
    blocked.write_text("x\n")
    blocked.chmod(0)
    try:
        with pytest.raises(_grader_tools.RestoreFailed):
            _rebuild(pristine, agent, tmp_path)
    finally:
        blocked.chmod(0o600)


def test_restore_repeats_the_rebuild(repos, tmp_path: Path):
    pristine, agent = repos
    (agent / "pkg" / "core.py").write_text("FIXED = 1\n")
    status, _, patch = _rebuild(pristine, agent, tmp_path)
    assert status == "ok"
    (agent / "pkg" / "core.py").write_text("tampered\n")
    (agent / "stray.txt").write_text("left by a test run\n")
    error = _grader_tools.restore(
        agent=str(agent), pristine=str(pristine), patch=patch.encode(), timeout=60
    )
    assert error is None
    assert (agent / "pkg" / "core.py").read_text() == "FIXED = 1\n"
    assert not (agent / "stray.txt").exists()


@pytest.mark.parametrize(
    ("path", "test"),
    [
        ("pkg/core.py", False),
        ("tests/helpers.py", True),
        ("Tests/helpers.py", True),
        ("pkg/zz_test.go", True),
        ("pkg/testdata/golden.json", True),
        ("src/a.test.ts", True),
        ("src/test_util.js", True),
        ("src/test_util.ts", True),
        ("src/__tests__/a.js", True),
        ("conftest.py", True),
        ("docs/conftest.py", True),
        ("docs/tests/example.py", False),
        ("docs/test_example.py", False),
        ("crate/tests/it.rs", True),
        ("pkg/testing.py", False),
        ("src/__snapshots__/a.test.ts.snap", True),
        ("src/a.snap", True),
        ("src/snapshot.py", False),
    ],
)
def test_test_paths(path, test):
    assert _grader_tools.is_test_path(path) is test


def test_test_file_changes_are_reverted(repos, tmp_path: Path):
    pristine, agent = repos
    (agent / "pkg" / "core.py").write_text("def clamp(x):\n    return 0\n")
    (agent / "pkg" / "zz_test.go").write_text("package pkg\nfunc TestMain() {}\n")
    (agent / "tests" / "helpers.py").write_text("import os\nos._exit(0)\n")
    (agent / "conftest.py").write_text("pytest_plugins = []\n")
    (agent / "tests" / "test_core.py").write_text("def test_x():\n    assert True\n")
    status, _, patch = _rebuild(pristine, agent, tmp_path)
    assert status == "ok"
    assert "pkg/core.py" in patch
    for path in ("zz_test.go", "helpers.py", "test_core.py", "conftest.py"):
        assert path not in patch
    assert (agent / "tests" / "test_core.py").read_text() == "def test_x():\n    pass\n"


def _unrenameable(monkeypatch):
    def refuse(source, target):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(_grader_tools.os, "rename", refuse)


def test_move_aside_deletes_what_it_cannot_rename(tmp_path: Path, monkeypatch):
    agent = tmp_path / "agent"
    (agent / "a" / "b").mkdir(parents=True)
    (agent / "a" / "b" / "f.py").write_text("x\n")
    (agent / "top.py").write_text("y\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("kept\n")
    (agent / "a" / "link").symlink_to(outside)  # removed, never followed
    _unrenameable(monkeypatch)
    _grader_tools._move_aside(str(agent), str(tmp_path / "aside"), time.monotonic() + 60)
    assert list(agent.iterdir()) == []
    assert (outside / "keep").read_text() == "kept\n"


def test_move_aside_deletion_past_its_deadline_is_too_large(tmp_path: Path, monkeypatch):
    agent = tmp_path / "agent"
    (agent / "flood").mkdir(parents=True)
    for index in range(50):
        (agent / "flood" / str(index)).write_text("")
    with pytest.raises(_grader_tools.Rejected) as error:
        _grader_tools._remove_within(str(agent / "flood"), time.monotonic() - 1)
    assert str(error.value) == "too_large"
    # Through _move_aside: the rename fails, the deletion runs out of time.
    clock = iter([0.0, 0.0] + [1e9] * 1000)
    monkeypatch.setattr(_grader_tools.time, "monotonic", lambda: next(clock))
    _unrenameable(monkeypatch)
    with pytest.raises(_grader_tools.Rejected) as error:
        _grader_tools._move_aside(str(agent), str(tmp_path / "aside"), 10.0)
    assert str(error.value) == "too_large"
