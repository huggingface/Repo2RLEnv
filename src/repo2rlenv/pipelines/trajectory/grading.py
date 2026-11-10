"""Trajectory adapters for the native source-only, separate verifier.

Keep the recorded project path as an alias; the submission boundary is always
/workspace. No interpreter, installed package or process crosses that boundary.
"""

from __future__ import annotations

import difflib
import posixpath
import shlex
from pathlib import PurePosixPath

from repo2rlenv.pipelines._eval_script import make_unified_diff
from repo2rlenv.pipelines._mutants import make_mutants
from repo2rlenv.pipelines.pr_runtime import (
    _runtime_aux_files,
    build_eval_script,
    ensure_system_python,
    separate_head,
    separate_verifier_files,
    stash_pristine,
)

_BASE_TOKEN = "R2E_TRAJECTORY_BASE_SHA"


def _image_base_script(*args, language=None, **kwargs) -> str:
    """Resolve the synthetic base from the fresh image, never the agent's git tree.

    Unlike PR tasks, trajectories have no upstream SHA at generation time. The
    native builder quotes a plain identifier unchanged; replace only that owned
    placeholder with a quoted shell expansion after the trusted prologue.
    """
    head = separate_head(language)
    script = build_eval_script(_BASE_TOKEN, *args, language=language, **kwargs)
    assert script.startswith(head)
    body = script[len(head) :].replace(_BASE_TOKEN, '"$R2E_BASE"')
    return head + 'R2E_BASE="$(git -C /opt/r2e/pristine rev-parse HEAD)"\n' + body


def project_image(root: str) -> str:
    """Finish a task image with a single synthetic base and a pristine copy."""
    path = PurePosixPath(root)
    reserved = (
        "/usr",
        "/bin",
        "/sbin",
        "/etc",
        "/opt",
        "/tests",
        "/solution",
        "/logs",
        "/dev",
        "/proc",
        "/sys",
        "/tmp",
    )
    if (
        not path.is_absolute()
        or str(path) != root
        or ".." in path.parts
        or len(path.parts) < 2
        or root in {"/root", "/home"}
        or any(path.is_relative_to(prefix) for prefix in reserved)
        or any(ord(c) < 32 for c in root)
    ):
        raise ValueError(f"Unsupported project root: {root!r}")
    q = shlex.quote(root)
    alias = ""
    if not path.is_relative_to("/workspace"):
        alias = f"RUN mkdir -p /workspace && cp -a {q}/. /workspace/ && rm -rf {q} && ln -s /workspace {q}\n"
    parents = " ".join(shlex.quote(str(p)) for p in path.parents if str(p) != "/")
    traversal = f"RUN chmod o+x {parents}\n" if parents else ""
    return (
        ensure_system_python()
        + "RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*\n"
        + alias
        + traversal
        + "WORKDIR /workspace\n"
        + "RUN find . -name .git -prune -exec rm -rf {} + && git init -q "
        "&& printf '.venv/\\nvenv/\\nnode_modules/\\n__pycache__/\\n' > .git/info/exclude "
        "&& git add -A && git -c user.name=Task -c user.email=task@example.invalid commit -qm base --allow-empty\n"
        + stash_pristine()
        + f"WORKDIR {root}\n"
    )


def project_prefix(root: str) -> str:
    path = PurePosixPath(root)
    return (
        str(path.relative_to("/workspace"))
        if path != PurePosixPath("/workspace") and path.is_relative_to("/workspace")
        else ""
    )


def _reward_policy(script: str, mode: str) -> str:
    """Retain the family's binary/regression policy after the native safety verdict."""
    suffix = "r2e_open_logs\nexit 0\n"
    assert script.endswith(suffix)
    return (
        script.removesuffix(suffix)
        + f""""$R2E_PY" -B -I -S - <<'R2E_REWARD_POLICY'
import json
from pathlib import Path
path = Path("/logs/verifier/reward-details.json")
data = json.loads(path.read_text())
if data.get("evaluation_status") == "graded" and (
    ({mode!r} == "binary" and data.get("reward") != 1.0)
    or ({mode!r} == "no_regressions" and data.get("p2p_rate", 1.0) != 1.0)
):
    data["reward"] = 0.0
    data["resolved"] = False
    path.write_text(json.dumps(data))
    Path("/logs/verifier/reward.txt").write_text("0.000000\\n")
R2E_REWARD_POLICY
"""
        + suffix
    )


def python_verifier_files(tests: str, new: set[str], existing: set[str]) -> dict[str, str]:
    """Grade the exact calibrated tests with the shared F2P/P2P and mutant verifier."""
    hidden = "test_hidden.py"
    f2p = sorted(f"{hidden}::{name}" for name in new)
    p2p = sorted(f"{hidden}::{name}" for name in existing)
    mutants = make_mutants(f2p, lambda path: tests, allowed_files={hidden}, seed=tests, decoys=p2p)
    if mutants is None:
        raise ValueError("Generated tests have no supported contradictory assertions")
    patch = (
        f"diff --git a/{hidden} b/{hidden}\nnew file mode 100644\n"
        + "".join(
            difflib.unified_diff(
                [],
                tests.rstrip("\n").splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{hidden}",
            )
        )
        + "\n"
    )
    return {
        **_runtime_aux_files(f2p, p2p),
        **separate_verifier_files(mutants),
        "tests/test_hidden.py": tests,
        "tests/test.sh": _reward_policy(
            _image_base_script(
                patch,
                [
                    "python -m pytest -v -p no:cacheprovider --timeout=60 "
                    + " ".join(shlex.quote(node) for node in f2p + p2p)
                ],
                language="python",
                fail_to_pass=f2p,
                pass_to_pass=p2p,
                run_timeout=600,
                copy_timeout=120,
                mutants=mutants,
            ),
            "no_regressions",
        ),
    }


def repair_verifier_files(
    root: str, command: str, protected: dict[str, str], f2p: list[str], p2p: list[str]
) -> dict[str, str]:
    """Use the repository grader; build/lint checks retain an exit-code reward."""
    prefix = project_prefix(root)
    files = {posixpath.join(prefix, path): content for path, content in protected.items()}

    # pytest reports paths relative to its invocation directory; the native mutant
    # builder must map those IDs back into the canonical workspace.
    def split(test: str):
        parts = test.split("::")
        return (
            (posixpath.join(prefix, parts[0]), tuple(p.split("[", 1)[0] for p in parts[1:]))
            if len(parts) > 1
            else None
        )

    mutants = make_mutants(
        f2p, files.get, allowed_files=set(files), seed=command, split=split, decoys=p2p
    )
    # Touch existing test files through a no-op diff so both control runs rewrite
    # the same protected paths with identical mtimes. The base already owns them.
    test_patch = "".join(
        make_unified_diff(content, content + "\n", path) for path, content in files.items()
    )
    return {
        **_runtime_aux_files(f2p, p2p),
        **separate_verifier_files(mutants),
        "tests/test.sh": _reward_policy(
            _image_base_script(
                test_patch,
                [command],
                fail_to_pass=f2p,
                pass_to_pass=p2p,
                run_timeout=600,
                copy_timeout=120,
                mutants=mutants,
            ),
            "binary",
        ),
    }
