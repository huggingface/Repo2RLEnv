"""Standalone private control audit; hash submissions without importing their code."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path, PurePosixPath


def submission_files(workspace: Path, contract: dict) -> dict[str, str | None]:
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
            with path.open("rb") as stream:
                result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def changed_files(before: dict, after: dict) -> dict:
    changes = {
        name: {"before": before.get(name), "after": after.get(name)}
        for name in sorted(before.keys() | after.keys())
        if before.get(name) != after.get(name)
    }
    if not changes:
        raise ValueError("Semantic probe left the collected submission unchanged")
    return changes


def main() -> None:
    contract, state, phase = sys.argv[1:]
    snapshot = submission_files(Path("/workspace"), json.loads(Path(contract).read_text()))
    if phase == "before":
        Path(state).write_text(json.dumps(snapshot, sort_keys=True))
    elif phase == "after":
        changes = changed_files(json.loads(Path(state).read_text()), snapshot)
        print("__QUALITY_PROBE_CHANGED_FILES__ " + json.dumps(changes, sort_keys=True))
        Path(state).unlink()
    else:
        raise ValueError("Unknown probe audit phase")


if __name__ == "__main__":
    main()
