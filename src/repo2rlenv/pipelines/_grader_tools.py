"""Trusted grading steps for native runtime tasks. Standalone and stdlib-only.

Root runs this with an image interpreter and ``-I -S``, in Harbor's separate
verifier and the same way during generation-time validation, before any
submitted code runs:

``rebuild``
    Harbor copies the agent's ``/workspace`` into the verifier. That copy is
    untrusted data: it carries the agent's virtualenv, ``node_modules``, build
    output and any shims. ``rebuild`` reads only what could be source: it skips
    paths the base commit ignores, dependency and build-output directories,
    directories the image ships outside version control and test files. Like
    SWE graders that diff tracked plus non-ignored untracked files, it leaves
    out untracked files that are binary or over 1 MiB (a stray build artifact
    is not source) and reports them. It compares each file with the image's
    checked-out copy (``/opt/r2e/pristine``), not with git's stored blobs, so
    a repository whose checkout differs from its blobs (``eol=crlf``,
    ``ident``) still applies. Sizes are checked before anything is read, and
    files are hashed as raw bytes (no attribute filters, no pathspec magic).
    The agent's tree is then moved aside, never deleted, and the diff applied
    to a fresh copy of the image's repository, which replaces ``/workspace``.
    Dependencies and caches always come from the image.

``check``
    For single-file submissions (code_instruct, equivalence_tests): the file
    must be a regular file within the size cap.

``restore``, ``settle``, ``keep``
    The mutant check (``_mutants``) runs the tests twice. ``restore`` puts the
    tree prepared for the first run back before the second; ``settle``
    rewrites every hidden test file the same way in both runs (run B with the
    mutants swapped in) and gives them one fixed mtime; ``keep`` copies a
    JUnit report the test user wrote into a root-only file, refusing links and
    FIFOs. They print ``ok``.

``rebuild`` and ``check`` print one word: ``ok`` or the reason the submission
is graded 0 (``too_large``, ``unsafe_symlink``, ``invalid``, ``apply_failed``,
``no_space``, ``not_regular``). Anything that goes wrong once the agent's tree
is being read is one of those reasons: the submission can cause it. Two exit
statuses mean the grader's own pieces failed and the caller leaves the run
ungraded: ``EXIT_PRE_READ`` (3) before the agent's tree is touched (no git, no
pristine copy) and ``EXIT_RESTORE`` (4) when copying the image's repository
fails. Any other failure (a crash, an out-of-memory kill) happened while the
agent's tree was being read, and the caller grades it 0.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Directories that hold installed dependencies or caches, never source. An
# untracked one is not read at all.
DEPENDENCY_DIRS = {
    ".git",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "site-packages",
}
# One test-path rule for generation and grading. Generation splits a PR's diff
# with it (test files become the hidden test patch); grading drops the
# submission's changes to such paths, as SWE-bench Pro V2 and Prime's
# pi_deepswe do: an added `TestMain`, `conftest.py` or `*.test.js` would be
# compiled or collected with the hidden suite, and an edited test file could
# rewrite pass-to-pass tests the hidden patch doesn't touch.
_TEST_DIR_NAMES = {"test", "tests", "testing", "e2e", "__tests__", "__snapshots__", "testdata"}
_DOC_ROOTS = {"docs", "doc", "documentation", "examples", "example"}
_TEST_FILE = re.compile(
    r"(^test_.*\.(py|[cm]?[jt]sx?)$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$|\.snap$)"
)
# Attributes for every git step on a rebuilt tree: a `.gitattributes` the agent
# wrote (working-tree-encoding, filters) must not change or break what is read.
NEUTRAL_ATTRIBUTES = "* -text -eol -working-tree-encoding -filter -ident\n"
# Bound on entries read from the agent's tree, so a flood of files is a graded
# `too_large` rather than a verifier timeout.
MAX_ENTRIES = 200_000
# An untracked file that looks binary (a NUL byte in its first 8000 bytes, git's
# own test) is left out of the diff and reported: a compiled binary or a
# downloaded fixture the agent forgot is not source. Untracked text of any size
# crosses and counts towards the caps, wherever it sits.
_BINARY_PROBE = 8000
# How many skipped paths `submission.json` lists (the count is always exact).
SKIPPED_LISTED = 10_000
_CHUNK = 1 << 16
EXIT_PRE_READ = 3
EXIT_RESTORE = 4
# Every hidden test file gets this mtime in both runs of the mutant check.


def is_test_path(path: str) -> bool:
    """A path the hidden suite could pick up as a test, fixture or test hook.

    Case-insensitive. `conftest.py` always counts; otherwise nothing under a
    documentation root (`docs/`, `examples/`, ...) does.
    """
    parts = [part.lower() for part in path.split("/") if part]
    if not parts:
        return False
    if parts[-1] == "conftest.py":
        return True
    if parts[0] in _DOC_ROOTS:
        return False
    return bool(_TEST_FILE.search(parts[-1])) or any(p in _TEST_DIR_NAMES for p in parts[:-1])


GIT_OPTIONS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.symlinks=true",
    "-c",
    "safe.directory=*",
)


class Rejected(Exception):
    """The submission is graded 0 for the reason in the message."""


class PreReadFailed(Exception):
    """The grader's own pieces failed before the agent's tree was touched."""


class RestoreFailed(Exception):
    """Copying the image's repository failed: grader infrastructure."""


class _Git:
    def __init__(self, git_dir: str, home: str, timeout: float) -> None:
        git = shutil.which("git", path=SAFE_PATH)
        if git is None:
            raise RuntimeError("git is not installed in the verifier image")
        self.git = git
        self.timeout = timeout
        self.env = {
            "PATH": SAFE_PATH,
            "HOME": home,
            "XDG_CONFIG_HOME": home,
            "GIT_DIR": git_dir,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }

    def __call__(
        self,
        *args: str,
        index: str | None = None,
        work_tree: str | None = None,
        data: bytes | None = None,
        check: bool = True,
        literal: bool = True,
        ok_codes: tuple[int, ...] = (0,),
    ) -> bytes:
        env = dict(self.env)
        if not literal:
            del env["GIT_LITERAL_PATHSPECS"]  # check-ignore refuses the flag
        if index is not None:
            env["GIT_INDEX_FILE"] = index
        command = [self.git, *GIT_OPTIONS]
        if work_tree is not None:
            command.append(f"--work-tree={work_tree}")
        result = subprocess.run(
            [*command, *args],
            input=data,
            env=env,
            cwd=work_tree or "/",
            capture_output=True,
            timeout=self.timeout,
        )
        if check and result.returncode not in ok_codes:
            raise RuntimeError(f"git {args[0]} failed: {result.stderr.decode(errors='replace')}")
        return result.stdout


def _split_z(raw: bytes) -> list[str]:
    return [os.fsdecode(item) for item in raw.split(b"\0") if item]


def _neutralize_attributes(git_dir: str) -> None:
    info = os.path.join(git_dir, "info")
    os.makedirs(info, exist_ok=True)
    with open(os.path.join(info, "attributes"), "w", encoding="utf-8") as handle:
        handle.write(NEUTRAL_ATTRIBUTES)


def _move_aside(agent: str, aside_root: str | None, deadline: float) -> str:
    """Move every entry of `agent` into a new directory; return its path.

    The agent's tree is not deleted when it can be renamed (deleting takes as
    long as the agent makes it take); the verifier container is discarded
    afterwards. An entry that can't be renamed (another filesystem) is deleted
    within the same deadline: the tree's size is the agent's choice, so a run
    past it is a graded `too_large`, not a verifier timeout.
    """
    parent = aside_root or (os.path.dirname(os.path.abspath(agent).rstrip("/")) or "/")
    os.makedirs(parent, mode=0o700, exist_ok=True)
    aside = tempfile.mkdtemp(prefix=".r2e-agent-tree-", dir=parent)
    with os.scandir(agent) as listing:
        names = [entry.name for entry in listing]
    for name in names:
        if time.monotonic() > deadline:
            raise Rejected("too_large")
        source = os.path.join(agent, name)
        try:
            os.rename(source, os.path.join(aside, name))
        except OSError:
            _remove_within(source, deadline)
    return aside


def _remove_within(path: str, deadline: float) -> None:
    """Delete `path` (a file, link or directory tree) or raise Rejected past `deadline`.

    Walks by directory descriptor, so neither depth nor links in the tree
    matter: a link is removed, never followed.
    """
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    for _top, dirnames, filenames, dir_fd in os.fwalk(path, topdown=False, follow_symlinks=False):
        for name in filenames + dirnames:
            if time.monotonic() > deadline:
                raise Rejected("too_large")
            try:
                os.unlink(name, dir_fd=dir_fd)
            except IsADirectoryError:
                os.rmdir(name, dir_fd=dir_fd)
            except PermissionError:  # Linux reports a directory as EPERM to unlink
                os.rmdir(name, dir_fd=dir_fd)
    os.rmdir(path)


class _Base:
    """The pristine copy's base commit: what the image ships, read once."""

    def __init__(self, git: _Git, pristine: str) -> None:
        if not os.path.isdir(os.path.join(pristine, ".git")):
            raise RuntimeError(f"no repository at {pristine}")
        _neutralize_attributes(os.path.join(pristine, ".git"))
        self.pristine = pristine
        self.tree = git("rev-parse", "HEAD^{tree}").decode().strip()
        fmt = git("rev-parse", "--show-object-format", check=False).decode().strip()
        self.algorithm = "sha256" if fmt == "sha256" else "sha1"
        self.entries: dict[str, tuple[str, str]] = {}
        for record in git("ls-tree", "-rz", "--full-tree", self.tree).split(b"\0"):
            if record:
                meta, path = record.split(b"\t", 1)
                mode, _kind, blob = meta.decode().split()
                self.entries[os.fsdecode(path)] = (mode, blob)
        self.dirs = {""}
        for path in self.entries:
            parent = os.path.dirname(path)
            while parent not in self.dirs:
                self.dirs.add(parent)
                parent = os.path.dirname(parent)


def rebuild(**options) -> str:
    """Rebuild the submission; return "ok" or the reason it is graded 0.

    Raises PreReadFailed before the agent's tree is touched and RestoreFailed
    when copying the image's repository fails: both leave the run ungraded.
    """
    with tempfile.TemporaryDirectory(prefix="r2e-submission-") as temporary:
        try:
            git = _Git(os.path.join(options["pristine"], ".git"), temporary, options["timeout"])
            base = _Base(git, options["pristine"])
        except Exception as exc:
            raise PreReadFailed(f"{type(exc).__name__}: {exc}") from exc
        report: dict = {"status": "ok"}
        try:
            _rebuild(report, git, base, temporary, **options)
        except RestoreFailed:
            _write_report(options["report_out"], {**report, "status": "restore_failed"})
            raise
        except Rejected as rejected:
            report["status"] = str(rejected)
        except subprocess.TimeoutExpired:
            report["status"] = "too_large"
        except Exception as exc:  # the agent's tree made a step fail
            report.update({"status": "invalid", "error": f"{type(exc).__name__}: {exc}"[:2000]})
    _write_report(options["report_out"], report)
    return report["status"]


def _open_regular(path: str):
    """A binary handle on a regular file, never following a link or blocking on a FIFO."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    handle = os.fdopen(descriptor, "rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        return None
    return handle


def _read_regular(path: str, limit: int | None = None) -> bytes | None:
    """A regular file's bytes, never following a link or blocking on a FIFO;
    None when it isn't a regular file or holds more than `limit` bytes."""
    handle = _open_regular(path)
    if handle is None:
        return None
    with handle:
        data = handle.read() if limit is None else handle.read(limit + 1)
    return None if limit is not None and len(data) > limit else data


def _same_bytes(first: str, second: str) -> bool:
    """Whether two regular files hold the same bytes, read in chunks."""
    a = _open_regular(first)
    b = _open_regular(second)
    try:
        if a is None or b is None:
            return False
        while True:
            left, right = a.read(_CHUNK), b.read(_CHUNK)
            if left != right:
                return False
            if not left:
                return True
    finally:
        for handle in (a, b):
            if handle is not None:
                handle.close()


def _looks_binary(path: str) -> bool:
    handle = _open_regular(path)
    if handle is None:
        return True
    with handle:
        return b"\0" in handle.read(_BINARY_PROBE)


class _File:
    """What the walk knows about one candidate path: no contents yet."""

    __slots__ = ("link", "mode", "mtime", "size")

    def __init__(self, info: os.stat_result, full: str) -> None:
        self.size = info.st_size
        self.mtime = info.st_mtime_ns
        if stat.S_ISLNK(info.st_mode):
            self.mode = "120000"
            self.link = os.fsencode(os.readlink(full))
        else:
            self.mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
            self.link = None


def _walk(
    git: _Git, base: _Base, agent: str, deadline: float, index: str, skipped: list[str]
) -> dict:
    """{path: _File} for every regular file or link in the agent's tree that could be source.

    Nothing is read here. Untracked paths the base's ignore rules ignore, that
    the image ships outside version control, or that are dependency and cache
    directories are dropped before anything is, and appended to `skipped`
    (a directory as `path/`).
    """
    pristine = base.pristine
    found: dict[str, _File] = {}
    untracked_files: list[str] = []
    frontier = [""]
    entries = 0
    while frontier:
        subdirs: list[str] = []
        candidates: list[str] = []
        for directory in frontier:
            with os.scandir(os.path.join(agent, directory) if directory else agent) as listing:
                for item in listing:
                    entries += 1
                    if entries > MAX_ENTRIES or time.monotonic() > deadline:
                        raise Rejected("too_large")
                    path = f"{directory}/{item.name}" if directory else item.name
                    if item.name == ".git" or base.entries.get(path, ("",))[0] == "160000":
                        continue  # repositories and submodules are not source
                    if is_test_path(path) or (
                        item.is_dir(follow_symlinks=False) and is_test_path(f"{path}/x")
                    ):
                        continue  # test files stay as the base has them
                    if item.is_dir(follow_symlinks=False):
                        if path in base.dirs:
                            subdirs.append(path)
                        elif (
                            item.name in DEPENDENCY_DIRS
                            # A directory the image ships untracked (a venv with
                            # no .gitignore entry) belongs to the environment.
                            or os.path.isdir(os.path.join(pristine, path))
                        ):
                            skipped.append(f"{path}/")
                        else:
                            candidates.append(path)
                        continue
                    info = item.stat(follow_symlinks=False)
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                        continue  # sockets, FIFOs, devices: not source
                    if path not in base.entries:
                        untracked_files.append(path)
                    found[path] = _File(info, item.path)
        if candidates:
            ignored = _ignored(git, pristine, [f"{path}/" for path in candidates])
            subdirs += [path for path in candidates if f"{path}/" not in ignored]
            skipped += [f"{path}/" for path in candidates if f"{path}/" in ignored]
        frontier = subdirs
    # The base's ignore rules decide what is source, not the agent's .gitignore.
    git("read-tree", base.tree, index=index)
    image_files = set(
        _split_z(
            git("ls-files", "-z", "--others", "--exclude-standard", index=index, work_tree=pristine)
        )
    )
    for path in sorted(
        _ignored(git, pristine, untracked_files) | (image_files & set(untracked_files))
    ):
        found.pop(path, None)
        skipped.append(path)
    return found


def _ignored(git: _Git, pristine: str, paths: list[str]) -> set[str]:
    """Which of `paths` the base's ignore rules ignore (a trailing / = a directory)."""
    if not paths:
        return set()
    # check-ignore can't take GIT_LITERAL_PATHSPECS; a leading "./" keeps a name
    # like ":(glob)x" from being read as pathspec magic.
    listed = b"\0".join(b"./" + os.fsencode(path) for path in paths)
    raw = git(
        "check-ignore",
        "--no-index",
        "-z",
        "--stdin",
        data=listed,
        work_tree=pristine,
        literal=False,
        ok_codes=(0, 1),  # 1: nothing ignored
    )
    return {path[2:] if path.startswith("./") else path for path in _split_z(raw)}


def _pristine_side(base: _Base, path: str) -> _File | None:
    """The image's checked-out copy of `path` (regular file or link), if any."""
    full = os.path.join(base.pristine, path)
    try:
        info = os.lstat(full)
    except FileNotFoundError:
        return None
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        return None
    return _File(info, full)


def _unchanged(base: _Base, agent: str, path: str, mine: _File) -> bool:
    """Whether the agent's file equals the image's checked-out copy of it."""
    theirs = _pristine_side(base, path)
    if theirs is None or theirs.mode != mine.mode:
        return False
    if mine.link is not None:
        return mine.link == theirs.link
    if mine.size != theirs.size:
        return False
    # A file whose size and exact (nanosecond) mtime match the image's copy was
    # not rewritten; faking that only hides the agent's own change. Anything
    # else (a copy that rounded mtimes, say) is compared byte by byte.
    if mine.mtime == theirs.mtime:
        return True
    return _same_bytes(os.path.join(agent, path), os.path.join(base.pristine, path))


def _hash_into(git: _Git, full: str, entry: _File) -> str:
    """Write `full` (a link's target text, or a file's raw bytes) as a blob."""
    if entry.link is not None:
        return git("hash-object", "-w", "--no-filters", "--stdin", data=entry.link).decode().strip()
    handle = _open_regular(full)
    if handle is None:
        raise Rejected("invalid")
    with handle:
        result = subprocess.run(
            [git.git, *GIT_OPTIONS, "hash-object", "-w", "--no-filters", "--stdin"],
            stdin=handle,
            env=git.env,
            cwd="/",
            capture_output=True,
            timeout=git.timeout,
        )
    if result.returncode != 0:
        raise RuntimeError(f"git hash-object failed: {result.stderr.decode(errors='replace')}")
    return result.stdout.decode().strip()


def _rebuild(
    report: dict,
    git: _Git,
    base: _Base,
    temporary: str,
    *,
    agent: str,
    pristine: str,
    patch_out: str,
    report_out: str,
    max_bytes: int,
    max_files: int,
    timeout: float,
    copy_timeout: float | None = None,
    aside_root: str | None = None,
) -> None:
    del report_out, pristine
    deadline = time.monotonic() + timeout
    skipped: list[str] = []
    found = _walk(git, base, agent, deadline, os.path.join(temporary, "base.index"), skipped)
    changed: dict[str, _File] = {}
    size = 0
    for path, entry in sorted(found.items()):
        if time.monotonic() > deadline:
            raise Rejected("too_large")
        untracked = path not in base.entries
        # Only the first 8000 bytes are read here, whatever the file's size.
        if untracked and entry.link is None and _looks_binary(os.path.join(agent, path)):
            skipped.append(path)  # a stray binary artifact, not source
            continue
        if not untracked and _unchanged(base, agent, path, entry):
            continue
        changed[path] = entry
        size += entry.size
        # Sizes come from lstat: nothing over the caps is ever read.
        if len(changed) > max_files or size > max_bytes:
            raise Rejected("too_large")
    deleted = [
        path
        for path, (mode, _blob) in base.entries.items()
        if path not in found and mode != "160000" and not is_test_path(path)
    ]
    if len(changed) + len(deleted) > max_files:
        raise Rejected("too_large")
    for path, entry in changed.items():
        if entry.link is not None:
            target = entry.link.decode("utf-8", "surrogateescape")
            if (
                os.path.isabs(target)
                or os.path.normpath(os.path.join(os.path.dirname(path), target)).split("/")[0]
                == ".."
            ):
                raise Rejected("unsafe_symlink")

    # Both sides are raw bytes: the base side is the image's checked-out file,
    # not git's blob, so the patch applies to the copy the verifier restores.
    zeros = "0" * (64 if base.algorithm == "sha256" else 40)
    base_lines, removals, additions = [], [], []
    for path in sorted(set(changed) | set(deleted)):
        theirs = _pristine_side(base, path)
        if theirs is not None:
            oid = _hash_into(git, os.path.join(base.pristine, path), theirs)
            base_lines.append(f"{theirs.mode} {oid}\t{path}")
        elif path in base.entries:
            base_lines.append(f"0 {zeros}\t{path}")
        if path in changed:
            oid = _hash_into(git, os.path.join(agent, path), changed[path])
            additions.append(f"{changed[path].mode} {oid}\t{path}")
        else:
            removals.append(f"0 {zeros}\t{path}")
    # Removals first: a file can replace a directory and the other way round.
    final_lines = removals + additions
    trees = []
    for name, lines in (("base", base_lines), ("submitted", base_lines + final_lines)):
        index = os.path.join(temporary, f"{name}.raw.index")
        git("read-tree", base.tree, index=index)
        if lines:
            listed = b"".join(os.fsencode(line) + b"\0" for line in lines)
            git("update-index", "-z", "--index-info", data=listed, index=index)
        trees.append(git("write-tree", index=index).decode().strip())
    patch = git("diff", "--binary", "--full-index", "--no-renames", *trees)
    report.update(
        {
            "changed_files": len(changed) + len(deleted),
            "changed_bytes": size,
            "deleted_files": len(deleted),
            "skipped": sorted(skipped)[:SKIPPED_LISTED],
            "skipped_count": len(skipped),
        }
    )
    with open(patch_out, "wb") as handle:
        handle.write(patch)
    error = restore(
        agent=agent,
        pristine=base.pristine,
        patch=patch,
        timeout=max(1.0, deadline - time.monotonic()),
        copy_timeout=copy_timeout,
        aside_root=aside_root,
        report=report,
    )
    if error is not None:
        report["apply_error"] = error
        raise Rejected("apply_failed")
    # Each link stays inside the tree after the others are in place too: two
    # links that pass one by one (`z -> .`, `x -> z/..`) can still escape.
    root = os.path.realpath(agent)
    for path, entry in changed.items():
        if entry.link is not None:
            resolved = os.path.realpath(os.path.join(agent, path))
            if resolved != root and not resolved.startswith(root + os.sep):
                raise Rejected("unsafe_symlink")


def restore(
    *,
    agent: str,
    pristine: str,
    patch: bytes,
    timeout: float,
    copy_timeout: float | None = None,
    aside_root: str | None = None,
    report: dict | None = None,
) -> str | None:
    """Replace `agent` with the image's copy plus `patch`; return git's error, if any.

    Moving the agent's tree aside is bounded by `timeout` (graded `too_large`
    past it). Copying the image's repository is the grader's own step: a
    failure or a run past `copy_timeout` raises RestoreFailed, except a full
    disk, which only the agent's uploaded tree can cause (graded `no_space`).
    """
    started = time.monotonic()
    aside = _move_aside(agent, aside_root, started + timeout)
    copy_started = time.monotonic()
    try:
        result = subprocess.run(
            ["cp", "-a", f"{pristine}/.", f"{agent}/"],
            env={"PATH": SAFE_PATH, "LC_ALL": "C"},
            capture_output=True,
            timeout=copy_timeout or max(timeout, 600.0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RestoreFailed("copying the image's repository timed out") from exc
    if result.returncode != 0:
        if b"No space left" in result.stderr:
            raise Rejected("no_space")
        raise RestoreFailed(result.stderr.decode(errors="replace")[-500:])
    if report is not None:
        report["copy_seconds"] = round(time.monotonic() - copy_started, 3)
        report["agent_tree_aside"] = aside
    if not patch:
        return None
    git_dir = os.path.join(agent, ".git")
    _neutralize_attributes(git_dir)
    with tempfile.TemporaryDirectory(prefix="r2e-apply-") as temporary:
        applier = _Git(git_dir, temporary, timeout)
        result = subprocess.run(
            [applier.git, *GIT_OPTIONS, "apply", "--binary", "--whitespace=nowarn", "-"],
            input=patch,
            env=applier.env,
            cwd=agent,
            capture_output=True,
            timeout=timeout,
        )
    if result.returncode != 0:
        return result.stderr.decode(errors="replace")[-2000:]
    return None


def _write_report(path: str, report: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)


def _regular_file(root: str, relative: str) -> str:
    """`root/relative` if it is a regular file reached without any symlink."""
    parts = relative.split("/")
    if posixpath.isabs(relative) or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe path: {relative!r}")
    current = root
    info = os.lstat(root)
    for part in parts:
        current = os.path.join(current, part)
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"path crosses a symlink: {relative!r}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"not a regular file: {relative!r}")
    return current


def settle(
    root: str,
    paths: list[str],
    spec_path: str = "",
    mutated: bool = False,
    mtime: int | None = None,
) -> None:
    """Rewrite each hidden test file in place, then give all of them one mtime.

    Every test run goes through here, run B of the mutant check with `mutated`,
    so no timestamp or write order tells code under test which run it is in.
    `mtime` (one value per grading, default now) is newer than anything the
    image built, so mtime-based build tools still rebuild the hidden tests;
    validation runs the same step, so a suite it breaks never ships.
    """
    stamp = int(time.time()) if mtime is None else int(mtime)
    files: dict[str, str] = {}
    if mutated:
        with open(spec_path, encoding="utf-8") as handle:
            files = json.load(handle)["files"]
    written = []
    for relative in sorted(set(paths) | set(files)):
        try:
            path = _regular_file(root, relative)
        except FileNotFoundError:
            if relative in files:
                raise  # a mutated file must exist in both runs
            continue  # deleted by the hidden test patch
        own = _read_regular(path) or b""
        data = files[relative].encode("utf-8") if relative in files else own
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        written.append(path)
    for path in written:
        os.utime(path, (stamp, stamp), follow_symlinks=False)


def keep(source: str, destination: str, max_bytes: int) -> None:
    """Copy a file test code wrote into a root-only file; leave none if unusable.

    Runs after every process of the test user is gone. A link, FIFO, directory
    or oversized file is not copied: the verifier then finds no report.
    """
    try:
        data = _read_regular(source, max_bytes)
    except OSError:
        data = None
    if data is None:
        return
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def check_submission(path: str, max_bytes: int) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "ok"  # nothing submitted: the tests fail to import it, honestly
    if not stat.S_ISREG(info.st_mode):
        return "not_regular"
    if info.st_size > max_bytes:
        return "too_large"
    return "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="trusted grading steps")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("rebuild")
    build.add_argument("--agent", default="/workspace")
    build.add_argument("--pristine", default="/opt/r2e/pristine")
    build.add_argument("--patch-out", required=True)
    build.add_argument("--report-out", required=True)
    build.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    build.add_argument("--max-files", type=int, default=5000)
    build.add_argument("--timeout", type=float, default=180)
    build.add_argument("--copy-timeout", type=float, default=None)
    build.add_argument("--aside-dir", default=None)
    check = commands.add_parser("check")
    check.add_argument("--path", required=True)
    check.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024)
    again = commands.add_parser("restore")
    again.add_argument("--agent", default="/workspace")
    again.add_argument("--pristine", required=True)
    again.add_argument("--timeout", type=float, default=180)
    again.add_argument("--copy-timeout", type=float, default=None)
    again.add_argument("--aside-dir", default=None)
    still = commands.add_parser("settle")
    still.add_argument("--root", default="/workspace")
    still.add_argument("--path", action="append", default=[], dest="paths")
    still.add_argument("--spec", default="")
    still.add_argument("--mutated", action="store_true")
    still.add_argument("--mtime", type=int, default=None)
    copy = commands.add_parser("keep")
    copy.add_argument("--source", required=True)
    copy.add_argument("--destination", required=True)
    copy.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        if args.command == "rebuild":
            status = rebuild(
                agent=args.agent,
                pristine=args.pristine,
                patch_out=args.patch_out,
                report_out=args.report_out,
                max_bytes=args.max_bytes,
                max_files=args.max_files,
                timeout=args.timeout,
                copy_timeout=args.copy_timeout,
                aside_root=args.aside_dir,
            )
        elif args.command == "check":
            status = check_submission(args.path, args.max_bytes)
        elif args.command == "restore":
            # Back to the tree prepared before the first run: nothing the first
            # run wrote reaches the second. Its tree goes into a root-only
            # directory (`--aside-dir`), out of the second run's reach.
            try:
                restore(
                    agent=args.agent,
                    pristine=args.pristine,
                    patch=b"",
                    timeout=args.timeout,
                    copy_timeout=args.copy_timeout,
                    aside_root=args.aside_dir,
                )
                status = "ok"
            except Rejected as rejected:  # the first run's tree, or a full disk
                status = str(rejected)
        elif args.command == "settle":
            settle(args.root, args.paths, args.spec, args.mutated, args.mtime)
            status = "ok"
        else:
            keep(args.source, args.destination, args.max_bytes)
            status = "ok"
    except PreReadFailed as exc:  # before the agent's tree was touched: ungraded
        sys.stderr.write(f"R2E grader step failed before reading the submission: {exc}\n")
        return EXIT_PRE_READ
    except RestoreFailed as exc:  # the image's own repository could not be copied
        sys.stderr.write(f"R2E grader could not restore the image's repository: {exc}\n")
        return EXIT_RESTORE
    except Exception as exc:  # the grader's own pieces are missing: ungraded
        sys.stderr.write(f"R2E grader step failed: {type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write(f"{status}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
