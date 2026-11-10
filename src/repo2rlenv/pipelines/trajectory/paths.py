"""Validate untrusted file names before touching the controller's filesystem."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


def relative_path(value: str) -> str:
    """Require a portable, normalized relative file path (never a directory)."""
    parts = value.split("/")
    if (
        not value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part for part in parts)
    ):
        raise ValueError(f"Unsafe relative file path: {value!r}")
    return value


def within(root: Path, value: str) -> Path:
    """Resolve a validated name without following any existing symlink."""
    relative_path(value)
    if root.is_symlink():
        raise ValueError(f"Symlink output directory: {root}")
    root = root.resolve()
    target = root
    for part in value.split("/"):
        target = target / part
        if target.is_symlink():
            raise ValueError(f"Symlink in output path: {value!r}")
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"Output path escapes staging directory: {value!r}")
    return target
