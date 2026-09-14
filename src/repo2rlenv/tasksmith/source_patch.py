"""Recover newline-normalized PR patches without changing pinned source bytes."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath


def _blob(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def _crlf_text(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    remaining = content.replace(b"\r\n", b"")
    return b"\r\n" in content and not any(value in remaining for value in (b"\r", b"\n", b"\0"))


def reverse_crlf_patch(root: Path, patch: Path) -> bool:
    """Try exact CRLF hunk bytes after ordinary reverse application failed.

    Git blob IDs in the frozen diff must identify both the original CRLF head
    and the reconstructed preimage. Only a completely checked temporary copy
    replaces ``root``; the caller's pinned checkout and original patch stay intact.
    Mixed-ending/binary files and missing blob IDs are never normalized.
    """
    original = patch.read_bytes()
    blocks = re.split(rb"(?=^diff --git )", original, flags=re.MULTILINE)
    bindings = []
    for index, block in enumerate(blocks):
        header = re.match(rb"diff --git a/(.+) b/\1\n", block)
        if header is None:
            continue
        name = header[1].decode("utf-8")
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("CRLF patch paths must be repository-relative")
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        content = path.read_bytes()
        if not _crlf_text(content):
            continue
        ids = re.search(
            rb"^index ([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})(?: [0-7]{6})?\n", block, re.MULTILINE
        )
        if ids is None or not _blob(content).startswith(ids[2].decode()):
            raise ValueError(f"CRLF fallback requires the frozen postimage blob: {name}")
        lines = block.splitlines(keepends=True)
        in_hunk = False
        changed = False
        for number, line in enumerate(lines):
            if line.startswith(b"@@ "):
                in_hunk = True
            elif in_hunk and line[:1] in (b" ", b"+", b"-"):
                # Git removes the patch's final LF for this marker. Adding CR
                # would change an unterminated source line into a different blob.
                no_newline = number + 1 < len(lines) and lines[number + 1].startswith(
                    b"\\ No newline at end of file"
                )
                if line.endswith(b"\n") and not line.endswith(b"\r\n") and not no_newline:
                    lines[number] = line[:-1] + b"\r\n"
                    changed = True
        if changed:
            blocks[index] = b"".join(lines)
            bindings.append(
                {"path": name, "preimage": ids[1].decode(), "postimage": _blob(content)}
            )
    if not bindings:
        return False
    adapted = b"".join(blocks)
    with tempfile.TemporaryDirectory(prefix=".crlf-reverse-", dir=patch.parent) as directory:
        directory = Path(directory)
        staged = directory / "source"
        shutil.copytree(root, staged)
        adapted_path = directory / "source.diff"
        adapted_path.write_bytes(adapted)
        result = subprocess.run(
            ["git", "apply", "--reverse", str(adapted_path.resolve())],
            cwd=staged,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise ValueError("CRLF patch still does not apply exactly: " + result.stderr[-6000:])
        for binding in bindings:
            path = staged / binding["path"]
            expected = binding["preimage"]
            matches = (
                not path.exists()
                if set(expected) == {"0"}
                else (path.is_file() and _blob(path.read_bytes()).startswith(expected))
            )
            if not matches:
                raise ValueError(
                    f"CRLF fallback differs from the frozen preimage blob: {binding['path']}"
                )
        (patch.parent / "source-crlf.diff").write_bytes(adapted)
        (patch.parent / "source-crlf-fallback.json").write_text(
            json.dumps(
                {
                    "original_patch_sha256": hashlib.sha256(original).hexdigest(),
                    "applied_patch_sha256": hashlib.sha256(adapted).hexdigest(),
                    "operation": "reverse",
                    "files": bindings,
                },
                indent=2,
            )
            + "\n"
        )
        backup = directory / "original"
        root.rename(backup)
        try:
            staged.rename(root)
        except BaseException:
            backup.rename(root)
            raise
    return True
