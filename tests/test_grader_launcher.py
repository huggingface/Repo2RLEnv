"""The grader launcher's cleanup: what the test user leaves is found and removed.

These call the launcher's functions as the current user on a temporary tree
only (never `/`), so they run unprivileged on any POSIX host.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from repo2rlenv.pipelines import _grader_launcher as launcher

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("find") is None, reason="needs POSIX find"
)
UID = os.getuid() if hasattr(os, "getuid") else 0


def _deep_tree(root, depth: int, name: str = "d" * 200) -> None:
    """A chain of directories longer than PATH_MAX, built with relative steps."""
    descriptor = os.open(root, os.O_RDONLY)
    try:
        for _ in range(depth):
            os.mkdir(name, dir_fd=descriptor)
            child = os.open(name, os.O_RDONLY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        with open(os.open("leaf", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=descriptor), "w") as f:
            f.write("x")
    finally:
        os.close(descriptor)


def test_owned_paths_stop_at_the_first_owned_entry(tmp_path):
    area = tmp_path / "tmp"
    (area / "a" / "b").mkdir(parents=True)
    (area / "a" / "b" / "c.txt").write_text("x")
    (area / "keep").mkdir()
    (area / "keep" / "k.txt").write_text("x")
    found = launcher._owned_paths(UID, [str(area / "keep")], roots=[str(area)])
    # `area` itself is ours and is the first owned entry under the root.
    assert found == [str(area)]


def test_purge_removes_a_tree_deeper_than_path_max(tmp_path):
    area = tmp_path / "shared"
    area.mkdir()
    victim = area / "deep"
    victim.mkdir()
    _deep_tree(victim, depth=30)  # ~6 KB of path
    launcher._purge(UID, [], roots=[str(area)])
    assert not area.exists()


def test_purge_keeps_the_writable_directories(tmp_path):
    area = tmp_path / "root"
    (area / "workspace").mkdir(parents=True)
    (area / "workspace" / "src.py").write_text("x")
    (area / "stray").write_text("x")
    # The root itself must not be ours for `keep` to matter: list its children.
    launcher._purge(UID, [str(area / "workspace")], roots=[str(p) for p in area.iterdir()])
    assert (area / "workspace" / "src.py").exists()
    assert not (area / "stray").exists()


def test_ipc_tables_are_read_by_owner_and_creator(tmp_path):
    (tmp_path / "shm").write_text(
        "       key      shmid perms       size  cpid  lpid nattch   uid   gid  cuid  cgid\n"
        "         0         11   600       4096   10    10      0  20001 20001     0     0\n"
        "         0         12   600       4096   10    10      0      0     0 20001     0\n"
        "         0         13   600       4096   10    10      0      0     0     0     0\n"
    )
    (tmp_path / "msg").write_text(
        "       key      msqid perms      cbytes       qnum lspid lrpid   uid   gid  cuid  cgid\n"
        "         0          7   600           0          0     0     0 20001 20001 20001 20001\n"
    )
    (tmp_path / "sem").write_text(
        "       key      semid perms      nsems   uid   gid  cuid  cgid      otime      ctime\n"
    )
    assert sorted(launcher._ipc_objects(20001, str(tmp_path))) == [
        ("msg", 7),
        ("shm", 11),
        ("shm", 12),
    ]


def test_open_log_refuses_a_directory_the_test_user_can_read(tmp_path):
    shared = tmp_path / "logs"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(ValueError):
        launcher._open_log(str(shared / "output.log"))


def test_cleanup_failure_after_the_tests_is_reported_not_raised(monkeypatch, capsys):
    def run(*args, **kwargs):
        raise launcher.CleanupFailed("a fork bomb outlived the deadline")

    monkeypatch.setattr(launcher, "run", run)
    assert launcher.main(["--log", "/x/output.log", "--path", "/bin", "true"]) == 0
    assert capsys.readouterr().out.strip() == "cleanup_failed"


def test_failure_before_the_tests_start_exits_one(monkeypatch, capsys):
    def run(*args, **kwargs):
        raise RuntimeError("the grader launcher must start as root")

    monkeypatch.setattr(launcher, "run", run)
    assert launcher.main(["--log", "/x/output.log", "--path", "/bin", "true"]) == 1
    assert capsys.readouterr().out == ""


def test_purge_past_its_deadline_is_a_cleanup_failure(tmp_path, monkeypatch):
    def slow_purge(uid, keep, roots=None):
        import time

        time.sleep(30)

    monkeypatch.setattr(launcher, "_purge", slow_purge)
    with pytest.raises(RuntimeError, match="took over"):
        launcher._purge_within(UID, [], deadline=0.5)


def test_files_are_only_deleted_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_quiesce", lambda uid: calls.append("quiesce"))
    monkeypatch.setattr(launcher, "_remove_ipc", lambda uid: calls.append("ipc"))
    monkeypatch.setattr(launcher, "_purge_within", lambda *a: calls.append("purge"))
    launcher._cleanup(UID, [], None)  # the separate verifier: one run, discarded container
    assert calls == ["quiesce", "ipc"]
    calls.clear()
    launcher._cleanup(UID, [], 10.0)  # validation reruns in the same container
    assert calls == ["quiesce", "ipc", "purge"]


# --- the image check: nothing root runs later may be writable by the test user


def _image(tmp_path):
    stdlib = tmp_path / "usr" / "lib" / "python3"
    (stdlib / "json" / "__pycache__").mkdir(parents=True)
    (stdlib / "json" / "__init__.py").write_text("")
    (stdlib / "site-packages" / "big").mkdir(parents=True)
    bin_dir = tmp_path / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "git").write_text("#!/bin/sh\n")
    for path in (tmp_path / "usr", tmp_path / "usr" / "lib", stdlib, bin_dir):
        path.chmod(0o755)
    return stdlib, bin_dir


OTHER = UID + 1  # a uid that owns nothing here


def test_a_clean_image_has_nothing_unsafe(tmp_path):
    stdlib, bin_dir = _image(tmp_path)
    assert launcher.unsafe_image_paths(OTHER, [str(stdlib)], [str(bin_dir)]) == []


def test_a_world_writable_pycache_on_roots_import_path_is_unsafe(tmp_path):
    stdlib, bin_dir = _image(tmp_path)
    (stdlib / "json" / "__pycache__").chmod(0o777)
    found = launcher.unsafe_image_paths(OTHER, [str(stdlib)], [str(bin_dir)])
    assert found == [str(stdlib / "json" / "__pycache__")]


def test_paths_the_test_user_owns_or_can_write_are_unsafe(tmp_path):
    stdlib, bin_dir = _image(tmp_path)
    (bin_dir / "git").chmod(0o777)  # a binary root runs, writable by anyone
    found = launcher.unsafe_image_paths(OTHER, [str(stdlib)], [str(bin_dir)])
    assert found == [str(bin_dir / "git")]
    # Owned by the test user: unsafe whatever the mode.
    assert str(stdlib) in launcher.unsafe_image_paths(UID, [str(stdlib)], [str(bin_dir)])


def test_site_packages_is_not_on_roots_import_path(tmp_path):
    stdlib, bin_dir = _image(tmp_path)
    (stdlib / "site-packages" / "big").chmod(0o777)  # -S never imports from here
    assert launcher.unsafe_image_paths(OTHER, [str(stdlib)], [str(bin_dir)]) == []


def test_a_writable_ancestor_without_the_sticky_bit_is_unsafe(tmp_path):
    stdlib, bin_dir = _image(tmp_path)
    (tmp_path / "usr" / "lib").chmod(0o777)
    found = launcher.unsafe_image_paths(OTHER, [str(stdlib)], [str(bin_dir)])
    assert found == [str(tmp_path / "usr" / "lib")]
    (tmp_path / "usr" / "lib").chmod(0o1777)  # sticky: entries can't be swapped
    assert launcher.unsafe_image_paths(OTHER, [str(stdlib)], [str(bin_dir)]) == []


def test_a_log_over_the_cap_is_cut_with_a_marker(tmp_path):
    log = tmp_path / "out.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, b"x" * 5000)
        launcher._cap_log(descriptor, cap=1000)
    finally:
        os.close(descriptor)
    data = log.read_bytes()
    assert (
        data.startswith(b"x" * 1000)
        and data[1000:] == b"\n[R2E: test output truncated at 1000 bytes]\n"
    )
    small = tmp_path / "small.log"
    small.write_bytes(b"ok\n")
    descriptor = os.open(small, os.O_WRONLY)
    try:
        launcher._cap_log(descriptor, cap=1000)
    finally:
        os.close(descriptor)
    assert small.read_bytes() == b"ok\n"
