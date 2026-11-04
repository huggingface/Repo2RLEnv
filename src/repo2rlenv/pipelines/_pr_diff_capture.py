"""Standalone PR-diff capture without executing workspace Git configuration.

The emitted task uses a regular clone with a pinned base commit. Only its
object store is read: refs, index, config, hooks and replacement refs are not
used. This is a configuration boundary, not a sandbox for a malicious process
that can modify the verifier, binaries or object store while capture runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def capture(workspace: Path, base_commit: str) -> bytes:
    """Capture tracked and new files through a fresh private Git index."""
    if not re.fullmatch(r"[a-fA-F0-9]{40}", base_commit):
        raise ValueError("Expected the task's pinned SHA-1 base commit")
    objects = workspace.resolve() / ".git" / "objects"
    if not objects.is_dir():
        raise ValueError("The task's cloned Git object store is missing")
    git = shutil.which("git", path=os.defpath)
    if git is None:
        raise FileNotFoundError("System git is unavailable")
    with tempfile.TemporaryDirectory(prefix="r2e-diff-") as temporary:
        private = Path(temporary)
        # Do not forward GIT_CONFIG_PARAMETERS/COUNT, external diff programs,
        # system/global includes, user attributes, templates or credential env.
        env = {
            "PATH": os.defpath,
            "HOME": temporary,
            "XDG_CONFIG_HOME": temporary,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
        subprocess.run(
            [git, "init", "--quiet", "--template=", str(private / "repository")],
            env=env,
            cwd=private,
            check=True,
            capture_output=True,
            timeout=60,
        )
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = json.dumps(str(objects), ensure_ascii=False)
        command = [
            git,
            f"--git-dir={private / 'repository/.git'}",
            f"--work-tree={workspace.resolve()}",
        ]

        def run(*args: str) -> bytes:
            return subprocess.run(
                [*command, *args],
                env=env,
                cwd=workspace,
                check=True,
                capture_output=True,
                timeout=60,
            ).stdout

        run("read-tree", f"{base_commit}^{{tree}}")
        # All filter drivers are undefined in the private configuration. The
        # workspace's clean filters, fsmonitor and hooks cannot be invoked.
        run("add", "--all", "--", ".")
        return run("diff", "--cached", "--no-ext-diff", "--no-textconv", base_commit, "--")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("base_commit")
    args = parser.parse_args(argv)
    try:
        patch = capture(args.workspace, args.base_commit)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Do not echo child stderr: it may contain workspace-controlled data.
        sys.stderr.write(f"PR-diff capture failed ({type(exc).__name__})\n")
        return 1
    sys.stdout.buffer.write(patch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
