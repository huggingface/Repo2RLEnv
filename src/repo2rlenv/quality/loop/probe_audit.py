"""Standalone private control audit; hash submissions without importing their code."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath


def submission_files(workspace: Path, contract: dict) -> dict[str, dict | None]:
    """Track the mutable files collected by our repository-task verifier."""
    workspace = workspace.resolve()

    def checked(relative: str) -> Path:
        value = PurePosixPath(relative)
        if (
            not relative
            or value.is_absolute()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise ValueError("Probe submission paths must be canonical and relative")
        path = workspace / relative
        if any(component.is_symlink() for component in (path, *path.parents)):
            raise ValueError("Probe submissions must not contain symlinks")
        return path

    names = set(contract.get("submitted_files", []))
    for relative in contract.get("submitted_roots", []):
        root = checked(relative)
        names.update(path.relative_to(workspace).as_posix() for path in root.rglob("*.py"))
    names.difference_update(contract.get("immutable_assets", {}))
    result = {}
    for name in sorted(names):
        path = checked(name)
        if not path.exists():
            result[name] = None
        elif not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Probe submission is not a bounded regular file")
        else:
            content = path.read_bytes()
            fingerprint = hashlib.sha256(content).hexdigest()
            syntax = None
            if path.suffix == ".py":
                try:
                    syntax = hashlib.sha256(
                        ast.dump(ast.parse(content), include_attributes=False).encode()
                    ).hexdigest()
                except (SyntaxError, ValueError, UnicodeError):
                    # A task can target newer syntax than this audit interpreter.
                    # Retain byte comparison; the verifier still validates code.
                    pass
            result[name] = {"sha256": fingerprint, "syntax_sha256": syntax}
    return result


def changed_files(before: dict, after: dict, *, require_valid_python: bool = False) -> dict:
    def identity(value):
        return None if value is None else value["syntax_sha256"] or value["sha256"]

    changes = {
        name: {"before": before.get(name), "after": after.get(name)}
        for name in sorted(before.keys() | after.keys())
        if identity(before.get(name)) != identity(after.get(name))
    }
    if not changes:
        raise ValueError("Semantic probe left the collected submission unchanged")
    if require_valid_python:
        for name, change in changes.items():
            previous, current = change["before"], change["after"]
            if (
                name.endswith(".py")
                and current is not None
                and current["syntax_sha256"] is None
                and (previous is None or previous["syntax_sha256"] is not None)
            ):
                raise ValueError(f"Behavioral probe introduced unparseable Python: {name}")
    return changes


def main() -> None:
    contract, state, phase = sys.argv[1:]
    boundary = json.loads(Path(contract).read_text())
    snapshot = submission_files(Path("/workspace"), boundary)
    if phase == "before":
        Path(state).write_text(json.dumps(snapshot, sort_keys=True))
    elif phase == "after":
        changes = changed_files(
            json.loads(Path(state).read_text()),
            snapshot,
            require_valid_python=boundary.get("require_valid_python", False),
        )
        print("__QUALITY_PROBE_CHANGED_FILES__ " + json.dumps(changes, sort_keys=True))
        Path(state).unlink()
    else:
        raise ValueError("Unknown probe audit phase")


if __name__ == "__main__":
    main()
