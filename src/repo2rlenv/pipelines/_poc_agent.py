"""Agentic PoC-test synthesis for cve_patches.

Unlike one-shot prompt synthesis (the LLM sees only the prompt), this runs an
LLM **with shell access inside the vulnerable sandbox**: it reads any file,
inspects how existing tests import the package, writes the regression test,
runs pytest, sees it fail, and iterates. That makes the test actually import
correctly and reproduce the CVE. The pipeline then validates fail-pre/pass-post
on a clean checkout (`validate_pr`), so the agent's job is "produce a test that
fails on the unpatched code for the vulnerability's reason".

Reuses `bootstrap.docker.DockerSandbox` (the agent's bash tool) + `llm.complete`.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass

from repo2rlenv.bootstrap.docker import DockerSandbox
from repo2rlenv.llm import complete
from repo2rlenv.spec.input import LLMSpec

logger = logging.getLogger(__name__)

_SYSTEM = """You write a pytest regression test that reproduces a known security vulnerability.

The repository is checked out at /workspace at the VULNERABLE commit (BEFORE the fix).
Goal: create ONE test file that FAILS when run now (the bug is present) and that WOULD
PASS once the documented fix is applied. You have a Linux shell.

Reply with EXACTLY ONE action per turn and nothing else, in this format:

RUN
<a single shell command>

(use it to explore — `ls`, `cat`, `grep`, `find` — to see how existing tests import
the package, to write the test file with a heredoc, or to run pytest); OR

DONE
<path to the test file>

Send DONE only after you have RUN pytest on the file and SEEN it FAIL for the
vulnerability's reason.

Rules:
- Put the test in the repo's existing test directory; name it test_cve_poc.py.
- Plain `def test_*():` + assert. Deterministic. No network, no sleep, no fixtures you didn't define.
- Import the library exactly how existing tests do. Target the SPECIFIC behavior the fix
  changes (the now-rejected input / the corrected result), not unrelated APIs.
"""


@dataclass(slots=True)
class PoCResult:
    test_path: str
    test_code: str
    cost_usd: float


def _initial_prompt(vuln_desc: str, fix_diff: str) -> str:
    return (
        f"CVE / advisory:\n{vuln_desc[:3000]}\n\n"
        "The commit that FIXED it (study it to understand what behavior to test — "
        "do NOT apply it; the repo must stay vulnerable while you write the test):\n"
        f"```diff\n{fix_diff[:7000]}\n```\n\n"
        "Start by finding the test directory and how existing tests import the package."
    )


_ACTION_RE = re.compile(r"(?is)\b(RUN|DONE)\b\s*\n(.+)")


def _parse_action(text: str) -> tuple[str, str]:
    m = _ACTION_RE.search(text.strip())
    if not m:
        return "none", ""
    kind = m.group(1).lower()
    arg = m.group(2).strip()
    # for DONE, take the first line (the path); for RUN, the whole block is the command
    if kind == "done":
        arg = arg.splitlines()[0].strip().strip("`").strip()
    return kind, arg


def synthesize_poc_agentic(
    sandbox: DockerSandbox,
    *,
    parent_sha: str,
    vuln_desc: str,
    fix_diff: str,
    llm: LLMSpec,
    max_iterations: int = 14,
    max_spend_usd: float = 1.5,
    cmd_timeout: int = 180,
) -> tuple[PoCResult | None, float]:
    """Drive an agent to write a test that fails on the vulnerable code.

    Returns (PoCResult|None, total_cost_usd). The sandbox is reset to the
    vulnerable parent commit first; the caller owns sandbox lifecycle.
    """
    cost = 0.0
    # Position the working tree at the vulnerable (pre-fix) commit.
    sandbox.exec("git config --global --add safe.directory /workspace || true")
    reset = sandbox.exec(
        f"cd /workspace && git reset --hard {shlex.quote(parent_sha)} "
        f"&& git clean -fdx -e .venv -e venv -e __pycache__ -e .tox || true"
    )
    if not reset.ok:
        logger.debug("poc agent: reset to %s failed: %s", parent_sha, reset.truncated(400))

    history: list[str] = [_initial_prompt(vuln_desc, fix_diff)]
    for _step in range(max_iterations):
        resp = complete(
            llm, system=_SYSTEM, user="\n\n".join(history[-10:]), max_tokens=2000, temperature=0.2
        )
        cost += resp.cost_usd
        history.append("ASSISTANT:\n" + (resp.content or "").strip())
        kind, arg = _parse_action(resp.content or "")
        if kind == "done":
            content = sandbox.exec(f"cat {shlex.quote(arg)} 2>/dev/null").stdout
            if content.strip() and "def test" in content:
                # Normalize to a repo-root-relative path so the caller can wrap it
                # as a `git apply` new-file diff (the repo is mounted at /workspace).
                rel = arg.strip()
                for prefix in ("/workspace/", "workspace/", "./"):
                    if rel.startswith(prefix):
                        rel = rel[len(prefix) :]
                rel = rel.lstrip("/")
                return PoCResult(test_path=rel, test_code=content, cost_usd=cost), cost
            history.append(f"OBSERVATION: '{arg}' is missing or has no test. Keep working.")
        elif kind == "run":
            r = sandbox.exec(arg, timeout=cmd_timeout)
            history.append(f"OBSERVATION (exit {r.exit_code}):\n{r.truncated(max_chars=3500)}")
        else:
            history.append("OBSERVATION: reply with exactly `RUN\\n<cmd>` or `DONE\\n<path>`.")
        if max_spend_usd and cost >= max_spend_usd:
            logger.debug("poc agent: spend budget %.2f reached", max_spend_usd)
            break
    return None, cost
