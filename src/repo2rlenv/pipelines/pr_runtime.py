"""Sandbox-verified PR mining (SWE-bench-style).

For each merged PR within scope:
  1. Pull metadata + unified diff via `gh pr list` / `gh pr diff`
  2. Split the diff into `patch` (source files) and `test_patch` (test files)
     using the same keyword-on-path heuristic SWE-bench uses
  3. If validation enabled: run the bootstrap container twice (once with
     test_patch only, once with both patches) to compute FAIL_TO_PASS and
     PASS_TO_PASS sets — the verified oracle
  4. Emit a Harbor task with environment/Dockerfile (FROM <bootstrap_image>),
     tests/test.sh (the eval script), and solution/patch.diff (gold patch)

Unlike `pr_diff`, this pipeline requires a working Docker image from the
bootstrap phase. `cmd_generate` triggers `ensure_bootstrap()` automatically
when `requires_bootstrap=True`.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
This pipeline mirrors the data-collection + validation approach of:

  SWE-bench: Can Language Models Resolve Real-world Github Issues?
  (Jimenez et al., ICLR '24, arXiv:2310.06770)
  https://github.com/SWE-bench/SWE-bench        (MIT)

  SWE-bench-Live: A Live Benchmark for Issue Resolving
  (Zhang et al., NIPS '25, arXiv:2505.23419)
  https://github.com/microsoft/SWE-bench-Live   (MIT)

We adapt the patch-split heuristic (collect/utils.py:extract_patches),
the eval-script structure (harness/test_spec/utils.py:make_eval_script_list_common),
and the F2P/P2P grading semantics (harness/grading.py). No code is copied;
we don't depend on the `swebench` PyPI package.

Released under Apache-2.0 along with the rest of Repo2RLEnv.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from repo2rlenv import github
from repo2rlenv.auth import resolve_repo_token
from repo2rlenv.bootstrap.spec import BootstrapResult
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.github import GitHubError, PullRequestSummary
from repo2rlenv.gitlab import GitLabError
from repo2rlenv.pipelines._env_guard import (
    egress_guard_compose,
    fix_source_hosts,
    git_history_scrub,
)
from repo2rlenv.pipelines._grader_tools import is_test_path
from repo2rlenv.pipelines._grading_time import (
    UNMEASURED_RUN_SEC,
    VERIFIER_FLOOR_SEC,
    GradingTime,
)
from repo2rlenv.pipelines._mutants import Mutants
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.provider import provider_for
from repo2rlenv.sources import Capability
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import PRRuntimeOptions

logger = logging.getLogger(__name__)

# PR/MR data calls dispatch by source (GitHub vs GitLab); either can raise.
_PROVIDER_ERRORS = (GitHubError, GitLabError)


_CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#\d+\b", re.IGNORECASE)

# `git clean -fdx` excludes. Keep language dependency/build dirs so resetting
# to a PR's base_commit doesn't wipe installed deps the test suite needs:
#   node_modules (npm/yarn) · target (cargo) · vendor (go) · .venv/venv/.tox
#   (python) · .gradle/build (jvm). Without these, Node/Rust repos would yield
#   ZERO tasks because the suite can't even import its deps after the clean.
_GIT_CLEAN_EXCLUDES = (
    "-e .venv -e venv -e __pycache__ -e .tox "
    "-e node_modules -e target -e vendor -e .gradle -e .next -e .pytest_cache"
)

# Which issue does this PR close? Used to source the problem statement from
# the issue (bug report) instead of the PR body (fix description).
# Matches `Fixes #123`, `Closes #123`, AND the markdown-link form
# `fixes [#123](url)` that PR authors commonly use.
_LINKED_ISSUE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+\[?#(\d+)", re.IGNORECASE
)

# Boilerplate sections that bloat a PR body without describing the problem,
# OR leak the solution. We drop everything from the first such header on.
# Crucially this includes test sections: a "Tests added / updated" block
# names the exact FAIL_TO_PASS tests that grade the task (an eval leak).
_BODY_NOISE_HEADER_RE = re.compile(
    r"^\s*#{0,6}\s*"
    r"(?:checklist|change\s*log|changelog|release\s*notes?|how\s+to\s+test|"
    r"test\s*plan|tests?\s+added(?:\s*/?\s*updated)?|tests?\s+added\s+or\s+updated|"
    r"testing|types?\s+of\s+changes?|pr\s+checklist|reviewer\s+notes?)\s*:?\s*$",
    re.IGNORECASE,
)

# Solution-leak patterns stripped from the problem statement before use.
_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "cherry-pick c3535905", "backport commit abc1234", "commit deadbeef1234"
    re.compile(
        r"\b(?:cherry[-\s]?pick(?:ed|ing|s)?|back[-\s]?port(?:ed|ing|s)?|commit)\b"
        r"[^\n]*?\b[0-9a-f]{7,40}\b",
        re.IGNORECASE,
    ),
    # Bare full 40-char SHAs (almost always a git ref pointing at the fix)
    re.compile(r"\b[0-9a-f]{40}\b"),
    # Markdown links to a github PR/issue/commit (point straight at the fix)
    re.compile(r"\[[^\]]*\]\(https?://github\.com/[^\s)]*?/(?:pull|issues|commit)/[^\s)]*\)"),
    # Bare github PR/issue/commit URLs (incl. redirect.github.com)
    re.compile(r"https?://(?:\w+\.)?github\.com/[^\s)]*?/(?:pull|issues|commit)/\S+"),
    # Closes/Fixes/Resolves/See/Refs #N (and cross-repo owner/repo#N)
    re.compile(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|see|refs?|ref|follow[-\s]?up(?:\s+to)?)\b"
        r"[:\s]*(?:[\w.-]+/[\w.-]+)?#\d+",
        re.IGNORECASE,
    ),
    # Markdown issue/PR refs `[#1234](url)` and bare `[#1234]`
    re.compile(r"\[#\d+\]\([^)]*\)"),
    # Parenthesized PR/issue ref at the end of a commit subject — common
    # "merged via squash" trailer: `Fix X (#1234)`. Strip incl. preceding
    # whitespace so the title cleans up to "Fix X".
    re.compile(r"\s*\(#\d+\)"),
    # Cross-repo issue refs without a closes keyword (`gorilla#739`).
    # Distinct from owner/repo#N (which has a slash) — that's already
    # covered by the closes pattern above when paired with a keyword.
    re.compile(r"\b[a-zA-Z][\w.-]*#\d+\b"),
)


def _linked_issue_number(pr_body: str) -> int | None:
    """Return the issue number this PR closes, if any (`Closes #123`)."""
    m = _LINKED_ISSUE_RE.search(pr_body or "")
    return int(m.group(1)) if m else None


def _strip_info_leak(text: str) -> str:
    """Remove solution-pointing references (SHAs, fix-PR links, #refs)."""
    out = text
    for pat in _LEAK_PATTERNS:
        out = pat.sub("", out)
    return out


# A markdown comment block (PR templates wrap guidance in <!-- ... -->).
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Collapse 3+ blank lines down to a single blank line.
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
# Cap the instruction body so a 5000-word PR essay doesn't dominate context.
_MAX_BODY_CHARS = 4000


def _reflow_pr_body(body: str) -> str:
    """Tidy a verbose PR body into a focused problem statement.

    - drop HTML comment blocks (PR-template guidance)
    - drop everything from the first checklist/changelog/template header on
    - collapse runs of blank lines
    - cap length

    Conservative: only trims obvious boilerplate. Never rewrites prose (no
    LLM call here — deterministic + cheap).
    """
    body = _HTML_COMMENT_RE.sub("", body)
    kept: list[str] = []
    for line in body.splitlines():
        if _BODY_NOISE_HEADER_RE.match(line):
            break  # the rest is checklist/changelog noise
        kept.append(line)
    out = _MULTI_BLANK_RE.sub("\n\n", "\n".join(kept)).strip()
    if len(out) > _MAX_BODY_CHARS:
        out = out[:_MAX_BODY_CHARS].rstrip() + "\n\n…(truncated)"
    return out


# Path-component classifier for "is this a test file?".
#
# One rule for which paths are tests, shared with the separate verifier (it
# drops a submission's changes to them), so validation and grading agree.
_path_is_test = is_test_path


# Match `diff --git a/<path> b/<path>` block boundaries to split a unified diff
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def split_patch_and_test_patch(unified_diff: str) -> tuple[str, str]:
    """Split a PR's unified diff into (source patch, test patch).

    SWE-bench rule: a file hunk goes into `test_patch` iff its path contains
    one of `test/tests/e2e/testing`; everything else goes into `patch`.

    We walk the diff by `diff --git` markers (one per file in the PR) so we
    keep each file's hunks intact.
    """
    if not unified_diff.strip():
        return "", ""

    # Find each "diff --git a/X b/Y" header and the byte offset where its block starts
    matches = list(_DIFF_HEADER_RE.finditer(unified_diff))
    if not matches:
        # Empty / malformed — return whole thing as patch
        return unified_diff, ""

    patch_parts: list[str] = []
    test_parts: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(unified_diff)
        block = unified_diff[start:end]
        path_a = m.group(1)
        path_b = m.group(2)
        # If EITHER path looks like a test file (covers renames, new files), it's a test
        if _path_is_test(path_a) or _path_is_test(path_b):
            test_parts.append(block)
        else:
            patch_parts.append(block)

    return "".join(patch_parts), "".join(test_parts)


def _build_instruction(
    pr: PullRequestSummary,
    owner: str = "",
    name: str = "",
    *,
    token: str | None = None,
    provider=github,
) -> str:
    """Build the task instruction (problem statement).

    Sources the problem from the **linked issue** (the bug *report*) when the
    PR closes one, falling back to the PR body otherwise. The PR body is
    written by the fixer and routinely leaks the solution (commit SHAs to
    cherry-pick, the fix approach, the names of the grading tests); the issue
    describes the *symptom*, which is what we want the agent to work from.
    This mirrors SWE-bench, which builds problem statements from issue text.

    Whatever text we use is then run through the info-leak strip + reflow so
    stray fix-PR links / #refs / SHAs / test-section noise don't leak through.
    """
    title = pr.title
    body = pr.body or ""

    issue_num = _linked_issue_number(pr.body or "")
    if issue_num is not None and owner and name:
        fetched = provider.fetch_issue(owner, name, issue_num, token=token)
        if fetched:
            i_title, i_body = fetched
            title = i_title or pr.title
            body = i_body or body

    body = _CLOSES_RE.sub("", body)
    body = _strip_info_leak(body)
    body = _reflow_pr_body(body).strip()
    if not body:
        body = "(no description provided in source issue/PR)"
    title = _strip_info_leak(title).strip() or pr.title
    return (
        f"# Issue\n\n"
        f"**Title:** {title}\n\n"
        f"## Description\n\n"
        f"{body}\n\n"
        f"## Task\n\n"
        f"Modify the repository so that the issue described above is resolved. "
        f"The task's test suite verifies your patch by applying it on top of "
        f"the base commit `{pr.base_sha[:12]}` and running the modified tests."
    )


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


# PR titles that signal a non-bug chore — not a meaningful SWE task, and their
# bodies routinely leak the fix (commit SHA to cherry-pick, the source PR).
_NON_BUG_TITLE_RE = re.compile(
    r"\b(?:back[-\s]?port|cherry[-\s]?pick|revert|"
    r"bump|release|changelog|forward[-\s]?merge|"
    r"prepare\s+(?:for\s+)?release|version\s+bump|re-?sync)\b"
    # merge-forward / branch-sync PRs: "merge branch", "merge stable into main",
    # "merge X into Y", "sync stable", "sync main", "merge master" — these are
    # broad branch syncs, not focused bug fixes, and produce huge noisy diffs.
    r"|\bmerge\b[^\n]*\binto\b"
    r"|\bmerge\s+(?:branch|stable|main|master|develop|upstream|release)\b"
    r"|\bsync\s+(?:stable|main|master|branch|develop|upstream)\b",
    re.IGNORECASE,
)


def _is_non_bug_pr(title: str) -> bool:
    """True if the PR title marks a backport / cherry-pick / release / revert."""
    return bool(_NON_BUG_TITLE_RE.search(title or ""))


def _verifier_source() -> str:
    """Read the standalone graded verifier's source for base64 embedding.

    Mirrors pr_diff: the verifier (`_pr_runtime_verifier.py`) runs inside
    the task container, so we read its source at gen time and bake it into
    `tests/test.sh` as a base64 blob.
    """
    return (Path(__file__).parent / "_pr_runtime_verifier.py").read_text(encoding="utf-8")


def build_environment_dockerfile(bootstrap_image: str, base_commit: str) -> str:
    """Build the per-task environment/Dockerfile.

    The bootstrap image has the repo at the bootstrap-time HEAD (whatever
    ref was used during `repo2rlenv bootstrap`). Each PR has its own
    `base_commit` — usually NOT bootstrap HEAD. If Harbor builds the agent
    image from the bootstrap image as-is, the agent + verifier see
    HEAD-state source files, but the gold patch (and any model patch) was
    written against `base_commit`-state files. Patches would fail to apply
    against the wrong line context.

    Fix: at build time, fetch `base_commit` (in case shallow clone doesn't
    have it) and reset the working tree to it. This makes Harbor's
    "apply model patch then run test.sh" flow correct.

    `bootstrap_image` should be the tag (e.g. `local/r2e-bootstrap/foo:abc`)
    for local-only bootstraps and the registry-qualified digest
    (`ghcr.io/owner/foo@sha256:...`) for pushed images. Docker BuildKit's
    `FROM <name>@sha256:...` syntax tries to fetch from a registry — local
    digest references don't work. The caller (`_build_task`) picks the
    right form based on `BootstrapResult.pushed_to_registry`.
    """
    return (
        f"# Auto-generated by Repo2RLEnv pr_runtime\n"
        f"FROM {bootstrap_image}\n"
        f"WORKDIR /workspace\n"
        f"# Defensive: ensure git is on PATH at build time. Bootstrap base\n"
        f"# images vary — some (Python slim) don't ship git; some agents\n"
        f"# install it; others don't. Re-installing is a no-op when already\n"
        f"# present. Tries apt-get first (Debian/Ubuntu), then apk (Alpine).\n"
        f"# Install git + ca-certificates. Minimal images (node:alpine) ship\n"
        f"# without CA certs, so HTTPS git fetch fails verification.\n"
        f"RUN (command -v git >/dev/null 2>&1 && [ -e /etc/ssl/certs/ca-certificates.crt ]) || \\\n"
        f"    (apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\\n"
        f"     && rm -rf /var/lib/apt/lists/*) || \\\n"
        f"    (apk add --no-cache git ca-certificates && update-ca-certificates) || true\n"
        f"# Defensive: the graded F2P/P2P verifier is Python.\n"
        f"# Language-specific bootstrap images (Go/Rust/Node) may not\n"
        f"# ship python3 — install it so test.sh can score F2P/P2P.\n"
        + ensure_system_python()
        + f"# Position the working tree at the PR's base commit so subsequent\n"
        f"# model-patch applications align with the line context the patch\n"
        f"# was authored against. The fetch is a no-op if the commit is\n"
        f"# already in the shallow clone.\n"
        f"RUN git config --global --add safe.directory /workspace \\\n"
        f"    && git fetch --depth 1 origin {base_commit} 2>/dev/null \\\n"
        f"       || git fetch --unshallow origin 2>/dev/null || true\n"
        f"RUN git reset --hard {base_commit} && git clean -fdx {_GIT_CLEAN_EXCLUDES}\n"
        # ANTI-CHEAT: the working tree is at base_commit, but .git still holds
        # the future (origin/main, tags, the fix commit + its hidden test),
        # which an agent can read offline. Strip it down to base_commit.
         + git_history_scrub(base_commit) + stash_pristine()
    )


def _path_dirs_for_language(language: str | None) -> list[str]:
    extras = {
        "go": ["/usr/local/go/bin", "$HOME/go/bin"],
        "rust": ["$HOME/.cargo/bin"],
        "node": ["/usr/local/lib/node_modules/.bin", "$HOME/.nvm/versions/node/*/bin"],
        "java": ["/usr/lib/jvm/default-java/bin"],
    }
    return extras.get((language or "").lower(), [])


def _test_config_reset(base_commit: str, python: str = "python3 -I", env_out: str = "") -> str:
    """Run the same isolated configuration guard during generation and grading.

    With `env_out` (a shell word), the guard writes base-derived settings there
    (CARGO_PROFILE_* pins) for `_guard_env` to export to the test command.
    """
    source = Path(__file__).with_name("_pytest_config_guard.py").read_text(encoding="utf-8")
    return (
        f"{python} - {shlex.quote(base_commit)} {env_out} <<'R2E_PYTEST_CONFIG_GUARD'\n"
        f"{source}\nR2E_PYTEST_CONFIG_GUARD"
    )


def _guard_env() -> str:
    """Shell line: export what the guard wrote (root-written, from base files)."""
    return '[ -s "$R2E_GUARD_ENV" ] && { set -a; . "$R2E_GUARD_ENV"; set +a; }\n'


# ---------------------------------------------------------------------------
# Separate verifier
# ---------------------------------------------------------------------------
# Harbor grades in a fresh container from the task image and copies only the
# agent's /workspace over (`environment_mode = "separate"`). Without `exclude`,
# Harbor copies the directory itself and runs nothing in the agent's container.
# The copy replaces the image's /workspace, so the image keeps its own copy of
# the repository and dependencies in PRISTINE; `_grader_tools.py rebuild` turns
# the agent's tree into a source diff and applies it to a fresh copy of that.
SEPARATE_VERIFIER: dict[str, str] = {"environment_mode": "separate", "user": "root"}
WORKSPACE_ARTIFACT: list[dict[str, str]] = [{"source": "/workspace"}]
PRISTINE = "/opt/r2e/pristine"
# Separate mode with artifacts sanitized through tar's `data` filter (absolute
# and escaping links, setuid bits and special files dropped) needs this Harbor.
MIN_HARBOR_VERSION = "0.25.0"
# Root's own steps never search a directory the test user can write.
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_PYTHONS = ("/usr/local/bin/python3", "/usr/bin/python3")
# The launcher's log and timing go here under fixed names, in a directory only
# root can read, and test.sh renames them afterwards: nothing the test command
# can see (its argv, /logs listings) differs from one run to another.
RUN_DIR = "/logs/verifier/run"


def verifier_config(timing: GradingTime | None) -> dict[str, object]:
    """`[verifier]` keys for a separate-verifier task, timeout from measured runs."""
    timeout = timing.verifier_timeout if timing is not None else VERIFIER_FLOOR_SEC
    return {**SEPARATE_VERIFIER, "timeout_sec": float(timeout)}


def ensure_system_python() -> str:
    """Dockerfile lines: a root-owned python3 at a fixed path for grading steps.

    A python3 found on PATH may be a virtualenv under /workspace, which the
    test user can write during grading.
    """
    present = " || ".join(f"[ -x {path} ]" for path in _PYTHONS)
    return (
        f"RUN {present} || \\\n"
        "    (apt-get update && apt-get install -y --no-install-recommends python3 \\\n"
        "     && rm -rf /var/lib/apt/lists/*) || \\\n"
        "    apk add --no-cache python3 || true\n"
    )


def stash_pristine() -> str:
    """Dockerfile lines: keep the scrubbed repository and its dependencies for grading.

    This adds the size of /workspace (source plus installed dependencies) to
    the image; see docs/pipelines/pr_runtime.md for measured numbers.
    """
    return (
        "# The separate verifier rebuilds /workspace from this copy plus the\n"
        "# agent's source diff, so the agent's environment never reaches grading.\n"
        f"RUN mkdir -p /opt/r2e && cp -a /workspace {PRISTINE} && chmod 700 /opt/r2e\n"
    )


def separate_verifier_files(mutants: Mutants | None = None) -> dict[str, str]:
    """The trusted grading helpers test.sh runs from /tests (and the mutant spec)."""
    here = Path(__file__).parent
    files = {
        "tests/launcher.py": (here / "_grader_launcher.py").read_text(encoding="utf-8"),
        "tests/grader_tools.py": (here / "_grader_tools.py").read_text(encoding="utf-8"),
    }
    if mutants is not None and mutants.ids:
        files["tests/mutants.json"] = mutants.spec()
    return files


def _fresh_copies(language: str | None) -> list[str]:
    """Toolchain homes the test command writes: a fresh copy per run (`VAR=DIR`).

    cargo takes a lock file and updates its registry cache in CARGO_HOME even
    offline. Each run gets its own copy, so the image's stays root-owned.
    """
    if (language or "").lower() == "rust":
        return ['CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"']
    return []


def _readable_dirs(language: str | None) -> list[str]:
    """Toolchain dirs under root's 0700 home that the test user only reads."""
    language = (language or "").lower()
    if language == "go":
        return ['"${GOMODCACHE:-${GOPATH:-$HOME/go}/pkg/mod}"']
    if language == "node":
        return ['"${NVM_DIR:-$HOME/.nvm}"']
    if language == "rust":
        # The rustup proxies in the CARGO_HOME copy run toolchains from here.
        return ['"${RUSTUP_HOME:-$HOME/.rustup}"']
    return []


def _access_flags(language: str | None) -> str:
    """Launcher flags: what the test command may write and what it may read."""
    flags = ["--writable /workspace"]
    flags += [f"--fresh-copy {spec}" for spec in _fresh_copies(language)]
    flags += [f"--readable {d}" for d in _readable_dirs(language)]
    return " ".join(flags)


def _child_path(language: str | None) -> str:
    """Shell line: the image's PATH (plus toolchain dirs), for the test command only."""
    dirs = ":".join(_path_dirs_for_language(language))
    prefix = f"{dirs}:" if dirs else ""
    return f'R2E_CHILD_PATH="{prefix}$PATH"\n'


def finish_function() -> str:
    """Shell function every separate-verifier test.sh defines first.

    `r2e_finish graded STATUS MESSAGE` writes reward 0 for a failure the
    submission can cause; were the run dropped instead, the bypass would cost
    the policy nothing. `r2e_finish ungraded STATUS MESSAGE` writes no reward
    file, for grader infrastructure the submission can't reach: Harbor records
    a verifier failure and RL consumers drop the rollout instead of training
    on 0. Both stop the script.
    """
    return (
        # Root-only while tests run; readable by everyone once the verdict is in
        # (reward-details.json, submission.json, the reports, the kept log).
        "r2e_open_logs() {\n"
        "  find /logs/verifier -xdev \\( -type d -exec chmod 755 {} + \\) "
        "-o \\( -type f -exec chmod 644 {} + \\)\n"
        "}\n"
        "r2e_finish() {\n"
        "  rm -f /logs/verifier/reward.txt /logs/verifier/reward.json\n"
        '  if [ "$1" = graded ]; then\n'
        '    echo "0.000000" > /logs/verifier/reward.txt\n'
        "    R2E_REWARD=0.0\n"
        "  else\n"
        "    R2E_REWARD=null\n"
        "  fi\n"
        '  printf \'{"reward": %s, "resolved": false, "parse_status": "%s", '
        '"eval_trustworthy": false, "evaluation_status": "%s"}\' '
        '"$R2E_REWARD" "$2" "$1" > /logs/verifier/reward-details.json\n'
        '  echo "R2E: $3 ($1: $2)" >&2\n'
        "  r2e_open_logs\n"
        "  exit 0\n"
        "}\n"
    )


def _finish_unless(rc_var: str, kind: str, status: str, what: str) -> str:
    """Shell line: `r2e_finish` when `$rc_var` is non-zero."""
    return f'[ "${rc_var}" -eq 0 ] || r2e_finish {kind} {status} {shlex.quote(what)}\n'


def _trusted_python() -> str:
    """Shell block: pick the image's own interpreter; ungraded without one."""
    candidates = " ".join(_PYTHONS)
    return (
        'R2E_PY=""\n'
        f'for p in {candidates}; do [ -x "$p" ] && {{ R2E_PY="$p"; break; }}; done\n'
        '[ -n "$R2E_PY" ]\nR2E_PY_RC=$?\n'
        + _finish_unless("R2E_PY_RC", "ungraded", "no_system_python", "no system python3")
    )


def rebuild_submission(copy_timeout: int | None) -> str:
    """Shell block: rebuild /workspace from the image's copy plus the agent's diff.

    Exit statuses of `grader_tools.py rebuild`: 3 (before the agent's tree is
    read) and 4 (copying the image's repository) are grader infrastructure,
    ungraded. A word other than `ok` is the submission's doing, and so is any
    other failure, an out-of-memory kill included: graded 0.
    """
    copy = f" --copy-timeout {int(copy_timeout)}" if copy_timeout else ""
    return (
        'R2E_SUBMISSION="$("$R2E_PY" -B -I -S "$SCRIPT_DIR/grader_tools.py" rebuild '
        f"--agent /workspace --pristine {PRISTINE}{copy} "
        "--patch-out /logs/verifier/submission.patch "
        '--report-out /logs/verifier/submission.json)"\n'
        "R2E_REBUILD_RC=$?\n"
        'case "$R2E_REBUILD_RC" in\n'
        "  0) ;;\n"
        "  3) r2e_finish ungraded submission_rebuild_failed "
        "'grader tools failed before reading the submission';;\n"
        "  4) r2e_finish ungraded submission_restore_failed "
        "'could not copy the image repository';;\n"
        "  *) r2e_finish graded submission_rebuild_crashed "
        "'reading the submission failed';;\n"
        "esac\n"
        '[ "$R2E_SUBMISSION" = ok ] || r2e_finish graded "submission_$R2E_SUBMISSION" '
        "'submission not graded as submitted'\n"
    )


def run_verifier(arguments: str) -> str:
    """Shell block: the verifier, with what its exit status means.

    3: a file the task ships in /tests is unreadable, ungraded. Any other
    failure (a crash, or the CPU limit while parsing what the tests printed)
    is the submission's output at work: graded 0.
    """
    return (
        f'"$R2E_PY" -B -I -S "$SCRIPT_DIR/verifier.py" {arguments}\n'
        "R2E_VERIFIER_RC=$?\n"
        '[ "$R2E_VERIFIER_RC" -eq 3 ] && r2e_finish ungraded verifier_error '
        "'grader inputs in /tests are unreadable'\n"
        '[ "$R2E_VERIFIER_RC" -eq 0 ] || r2e_finish graded log_unparseable '
        "'the test output could not be graded'\n"
    )


def separate_head(language: str | None) -> str:
    """The start of every separate-verifier test.sh, before the submission is read."""
    return (
        "#!/bin/bash\n"
        "set -uo pipefail\n"
        # The image's PATH (plus toolchain dirs) is for the test command only.
        f"{_child_path(language)}"
        f"export PATH={SAFE_PATH}\n"
        "hash -r\n"
        "umask 022\n"
        "SCRIPT_DIR=/tests\n"
        "mkdir -p /logs/verifier\n"
        # Root-only while tests run: the test user can't list or read the
        # launcher's log, its timing or anything graded later.
        "chown 0:0 /logs/verifier && chmod 700 /logs/verifier\n"
        f"rm -rf {RUN_DIR} && mkdir -m 700 {RUN_DIR}\n"
        # Hidden tests and expected test IDs: root only from here.
        'chown -R 0:0 "$SCRIPT_DIR" && chmod -R go-rwx "$SCRIPT_DIR"\n'
        + finish_function()
        + _trusted_python()
    )


def launch_tests(
    test_block: str,
    language: str | None,
    *,
    timeout: int | None,
    cwd: str = "/workspace",
    purge: bool = False,
) -> str:
    """Shell block: run `test_block` through the launcher as the test user.

    Sets TEST_EXIT_CODE and R2E_TIMED_OUT, and leaves the output in
    /logs/verifier/test_output.log. A failure of the launcher's cleanup after
    the tests ran is a graded 0: code under test can provoke it (a fork bomb,
    a directory tree too deep to delete). A launcher that can't start the
    tests at all is grader infrastructure: ungraded. With `purge` the launcher
    also deletes the test user's files outside /workspace (the mutant check's
    two runs must not share any), within its deadline.
    """
    flags = _access_flags(language)
    if timeout is not None:
        flags += f" --timeout {int(timeout)}"
    if purge:
        flags += " --purge"
    return (
        f'R2E_LAUNCH_OUT="$("$R2E_PY" -B -I -S "$SCRIPT_DIR/launcher.py" --log {RUN_DIR}/output.log '
        f'--seconds-out {RUN_DIR}/seconds --cwd {cwd} --path "$R2E_CHILD_PATH" {flags} '
        f'-- {shlex.quote(test_block)})"\n'
        "R2E_LAUNCH_RC=$?\n"
        "hash -r\n"
        f"mv -f {RUN_DIR}/output.log /logs/verifier/test_output.log 2>/dev/null\n"
        f"mv -f {RUN_DIR}/seconds /logs/verifier/run.seconds 2>/dev/null\n"
        "R2E_TIMED_OUT=0\n"
        '[ "$R2E_LAUNCH_RC" -eq 0 ] || R2E_LAUNCH_OUT=failed\n'
        'case "$R2E_LAUNCH_OUT" in\n'
        "  timeout) TEST_EXIT_CODE=124; R2E_TIMED_OUT=1;;\n"
        "  cleanup_failed) r2e_finish graded test_cleanup_failed "
        "'test processes or files outlived the run';;\n"
        "  unsafe_image) r2e_finish ungraded unsafe_image "
        "'a path root runs code from is writable by the test user';;\n"
        # Cleaning up before the tests start: after an earlier run of the
        # mutant check, what is left is that run's doing (graded 0).
        '  precleanup_failed) if [ "${R2E_RUNS:-0}" -gt 1 ]; then r2e_finish graded '
        "test_cleanup_failed 'an earlier test run left state that could not be removed'; "
        "else r2e_finish ungraded grader_launch_failed 'test command isolation failed'; fi;;\n"
        "  ''|*[!0-9]*) r2e_finish ungraded grader_launch_failed "
        "'test command isolation failed';;\n"
        '  *) TEST_EXIT_CODE="$R2E_LAUNCH_OUT";;\n'
        "esac\n"
    )


# The tree prepared for the first run, kept for the second (root only).
PREPARED = "/opt/r2e/prepared"


def settle_hidden(hidden: list[str], *, mutated: bool = False) -> str:
    """Shell command: rewrite the hidden test files with this grading's one mtime.

    Every run does this (`two_runs`' run B with the mutants), and validation
    does it too: the mtime is newer than the image's build artifacts, so
    mtime-based build tools rebuild the tests. `R2E_MTIME` is set once per grading.
    """
    paths = " ".join(f"--path {shlex.quote(path)}" for path in hidden)
    spec = ' --spec "$SCRIPT_DIR/mutants.json" --mutated' if mutated else ""
    return (
        f'"$R2E_PY" -B -I -S "$SCRIPT_DIR/grader_tools.py" settle --root /workspace {paths}'
        f'{spec} --mtime "$R2E_MTIME" >/dev/null'
    )


def settle_once(hidden: list[str]) -> str:
    """Shell block: the single run's `settle_hidden` (no mutant check)."""
    return (
        'R2E_MTIME="$(date +%s)"\n'
        + settle_hidden(hidden)
        + " || r2e_finish ungraded grader_setup_failed 'hidden test setup failed'\n"
    )


def two_runs(
    launch: str,
    hidden: list[str],
    claimed: str,
    *,
    keep_junit: str = "",
    copy_timeout: int | None = None,
) -> str:
    """Shell block: the mutant check's two test runs (see ``_mutants``).

    `launch` is one ``launch_tests`` block. Both runs start from the same
    prepared tree, have every hidden test file rewritten with one fixed mtime
    (run B with the mutants swapped in) and run through the launcher with the
    same arguments, in random order, so code under test can't tell which run
    it is in. Run A's log and timing end up where a single run's would; run
    B's in ``mutant_output.log`` and ``run_b.seconds``. Run B is skipped only
    when A ran first and `claimed` (a command printing 1 or 0) says A passed
    no mutated test: then B can't change the verdict; `claimed` reads run A's
    log at ``RUN_DIR/run_a.log`` (or its report at ``RUN_DIR/junit_a.xml``).
    With `keep_junit`, each run's JUnit report is copied out before the tree
    is reset. Between the runs the first run's tree is moved into a root-only
    directory and the prepared tree copied back (`copy_timeout`, as for the
    rebuild); `launch` must purge the test user's files elsewhere.
    """
    limit = f"/usr/bin/timeout {int(copy_timeout)} " if copy_timeout else ""
    copy = f" --copy-timeout {int(copy_timeout)}" if copy_timeout else ""

    def keep(name: str) -> str:
        if not keep_junit:
            return ""
        return (
            f'  "$R2E_PY" -B -I -S "$SCRIPT_DIR/grader_tools.py" keep --source {keep_junit} '
            f"--destination {RUN_DIR}/{name} >/dev/null\n"
        )

    return (
        # Single-file images have no /opt/r2e; it is root-only either way.
        f"mkdir -p {posixpath.dirname(PREPARED)} && chmod 700 {posixpath.dirname(PREPARED)} "
        f"&& rm -rf {PREPARED} && {limit}cp -a /workspace {PREPARED} "
        f"2>{posixpath.dirname(PREPARED)}/prepare.err\n"
        "R2E_PREPARE_RC=$?\n"
        # Only the agent's uploaded tree (kept aside on the same disk) can fill
        # it: a full disk here is a graded 0, as in the rebuild.
        f'if [ "$R2E_PREPARE_RC" -ne 0 ] && grep -q "No space left" '
        f"{posixpath.dirname(PREPARED)}/prepare.err; then r2e_finish graded submission_no_space "
        "'the disk filled up while preparing the test runs'; fi\n"
        + _finish_unless("R2E_PREPARE_RC", "ungraded", "mutant_setup_failed", "run setup failed")
        + 'R2E_RUNS=0; R2E_MTIME="$(date +%s)"\n'
        "r2e_reset() {\n"
        # After a run: its /workspace goes aside, out of the next run's reach,
        # and the prepared tree comes back. Copying it is the grader's own step
        # (exit 4, ungraded); anything else the first run caused: graded 0.
        '  if [ "$R2E_RUNS" -gt 0 ]; then\n'
        '    R2E_RESET="$("$R2E_PY" -B -I -S "$SCRIPT_DIR/grader_tools.py" restore '
        f'--pristine {PREPARED} --aside-dir {posixpath.dirname(PREPARED)}/aside{copy})"\n'
        "    R2E_RESET_RC=$?\n"
        '    [ "$R2E_RESET_RC" -eq 4 ] && r2e_finish ungraded submission_restore_failed '
        "'could not copy the prepared tree'\n"
        '    { [ "$R2E_RESET_RC" -eq 0 ] && [ "$R2E_RESET" = ok ]; } || r2e_finish graded '
        "test_cleanup_failed 'the test run left a tree that could not be reset'\n"
        "  fi\n"
        "  R2E_RUNS=$((R2E_RUNS + 1))\n"
        "}\n"
        "r2e_run_a() {\n"
        "  r2e_reset\n"
        f"  {settle_hidden(hidden)} || r2e_finish ungraded mutant_setup_failed "
        "'hidden test setup failed'\n"
        + launch
        + keep("junit_a.xml")
        + '  R2E_A_EXIT="$TEST_EXIT_CODE"; R2E_A_TIMED_OUT="$R2E_TIMED_OUT"\n'
        f"  mv -f /logs/verifier/test_output.log {RUN_DIR}/run_a.log 2>/dev/null\n"
        f"  mv -f /logs/verifier/run.seconds {RUN_DIR}/run_a.seconds 2>/dev/null\n"
        "}\n"
        "r2e_run_b() {\n"
        "  r2e_reset\n"
        f"  {settle_hidden(hidden, mutated=True)} "
        "|| r2e_finish ungraded mutant_setup_failed 'hidden test setup failed'\n"
        + launch
        + keep("junit_b.xml")
        + '  R2E_B_EXIT="$TEST_EXIT_CODE"; R2E_B_TIMED_OUT="$R2E_TIMED_OUT"\n'
        "  mv -f /logs/verifier/test_output.log /logs/verifier/mutant_output.log 2>/dev/null\n"
        "  mv -f /logs/verifier/run.seconds /logs/verifier/run_b.seconds 2>/dev/null\n"
        "}\n"
        "R2E_B_EXIT=1; R2E_B_TIMED_OUT=0; R2E_B_SKIPPED=0\n"
        "if [ $((RANDOM % 2)) -eq 0 ]; then\n"
        "  r2e_run_a\n"
        f'  if [ "$R2E_A_TIMED_OUT" = 0 ] && [ "$({claimed})" != 0 ]; then r2e_run_b; '
        "else R2E_B_SKIPPED=1; fi\n"
        "else\n"
        "  r2e_run_b\n"
        "  r2e_run_a\n"
        "fi\n"
        'TEST_EXIT_CODE="$R2E_A_EXIT"; R2E_TIMED_OUT="$R2E_A_TIMED_OUT"\n'
        f"mv -f {RUN_DIR}/run_a.log /logs/verifier/test_output.log 2>/dev/null\n"
        f"mv -f {RUN_DIR}/run_a.seconds /logs/verifier/run.seconds 2>/dev/null\n"
    )


def mutant_args(*, junit: bool = False) -> str:
    """Verifier flags for the mutant check's second run."""
    report = (
        f"--mutant-junit {RUN_DIR}/junit_b.xml "
        if junit
        else "--mutant-log /logs/verifier/mutant_output.log "
    )
    return (
        f'--mutants "$SCRIPT_DIR/mutants.json" {report}'
        '--mutant-exit-code "$R2E_B_EXIT" --mutant-timed-out "$R2E_B_TIMED_OUT" '
        '--mutant-skipped "$R2E_B_SKIPPED" --mutant-seconds /logs/verifier/run_b.seconds '
    )


def build_separate_eval_script(
    base_commit: str,
    test_patch: str,
    test_cmds: list[str],
    *,
    language: str | None = None,
    fail_to_pass: list[str] | None = None,
    pass_to_pass: list[str] | None = None,
    run_timeout: int | None = None,
    copy_timeout: int | None = None,
    mutants: Mutants | None = None,
) -> str:
    """`tests/test.sh` for Harbor's separate verifier (``SEPARATE_VERIFIER``).

    Runs as root on an image built with ``stash_pristine``, with
    ``separate_verifier_files`` in /tests:
      * root's own steps use a fixed PATH and the image's python3 with ``-B -I -S``;
      * ``grader_tools.py rebuild`` replaces the collected /workspace with a
        fresh copy of the image's repository plus the agent's source diff;
      * the test-file reset, the runner configuration guard (every runner) and
        the hidden test patch go on top;
      * the tests run as an unprivileged user through ``launcher.py``, which
        kills every process of that user and removes its IPC objects before
        the log is read; a run past ``run_timeout`` (``UNMEASURED_RUN_SEC``
        when validation measured nothing) scores 0;
      * with ``mutants`` (and an F2P oracle) the suite runs twice, as is and
        with the mutants swapped in (``two_runs``); the verifier checks the
        second run against the first.
    The verifier container is discarded, so files a single run left are not
    deleted and there is no cleanup reset.
    """
    del pass_to_pass  # graded from tests/p2p.json
    test_files = _files_in_patch(test_patch)
    reset = (
        f"for f in {' '.join(shlex.quote(f) for f in test_files)}; do "
        f'if git cat-file -e {base_commit}:"$f" 2>/dev/null; '
        f'then git checkout {base_commit} -- "$f"; '
        'else rm -f -- "$f"; fi; done'
        if test_files
        else ":"
    )
    heredoc = "EOF_R2E_TEST_PATCH"
    apply = (
        ""
        if not test_patch.strip()
        else (
            f"git apply --verbose --reject - <<'{heredoc}'\n{test_patch}\n{heredoc}\n"
            "R2E_APPLY_RC=$?\n"
            + _finish_unless(
                "R2E_APPLY_RC", "graded", "test_patch_apply_failed", "test patch failed to apply"
            )
        )
    )
    guard = _test_config_reset(base_commit, '"$R2E_PY" -B -I -S', '"$R2E_GUARD_ENV"')
    test_block = " && ".join(test_cmds) if test_cmds else "echo 'no test_cmds configured'"
    checked = bool(fail_to_pass) and mutants is not None and bool(mutants.ids)
    cmds_str = " ".join(test_cmds).replace("'", "'\\''")
    claimed = (
        '"$R2E_PY" -B -I -S "$SCRIPT_DIR/verifier.py" --claimed '
        f"--log {RUN_DIR}/run_a.log --test-cmds '{cmds_str}' "
        '--mutants "$SCRIPT_DIR/mutants.json"'
    )
    script = (
        separate_head(language) + rebuild_submission(copy_timeout) + "cd /workspace\n"
        "git config --global --add safe.directory /workspace\n"
        f"{reset} || true\n"  # tolerate test files that didn't exist at base
        # The configuration guard restores runner configuration from base and
        # exits 2 when the submission changed a setting it can't restore.
        'R2E_GUARD_ENV="$(mktemp)"\n'
        f"{guard}\n"
        "R2E_RESET_RC=$?\n"
        '[ "$R2E_RESET_RC" -eq 2 ] && r2e_finish graded runner_config_changed '
        "'the submission changed runner configuration'\n"
        + _finish_unless(
            "R2E_RESET_RC", "graded", "test_config_reset_failed", "test config reset failed"
        )
        + _guard_env()
        + apply
        + (
            two_runs(
                launch_tests(
                    test_block, language, timeout=run_timeout or UNMEASURED_RUN_SEC, purge=True
                ),
                test_files,
                claimed,
                copy_timeout=copy_timeout,
            )
            if checked
            else settle_once(test_files)
            + launch_tests(test_block, language, timeout=run_timeout or UNMEASURED_RUN_SEC)
        )
        + "tail -c 1048576 /logs/verifier/test_output.log 2>/dev/null\n"
    )
    if not fail_to_pass:
        # No F2P oracle (e.g. --skip-validation): the binary exit-code reward.
        return script + (
            "R2E_REWARD=0.0\n"
            '[ "$R2E_TIMED_OUT" = 0 ] && [ "$TEST_EXIT_CODE" -eq 0 ] && R2E_REWARD=1.0\n'
            'echo "$R2E_REWARD" > /logs/verifier/reward.txt\n'
            'printf \'{"reward": %s, "parse_status": "exit_code", '
            '"eval_trustworthy": false, "exit_code": %s, "timed_out": %s, '
            '"evaluation_status": "graded"}\' '
            '"$R2E_REWARD" "$TEST_EXIT_CODE" "$R2E_TIMED_OUT" '
            "> /logs/verifier/reward-details.json\n"
            "r2e_open_logs\n"
            "exit 0\n"
        )
    return script + (
        run_verifier(
            "--log /logs/verifier/test_output.log "
            + (mutant_args() if checked else "")
            + '--f2p "$SCRIPT_DIR/f2p.json" --p2p "$SCRIPT_DIR/p2p.json" '
            f"--test-cmds '{cmds_str}' --exit-code \"$TEST_EXIT_CODE\" "
            '--timed-out "$R2E_TIMED_OUT" --seconds /logs/verifier/run.seconds '
            "--out-dir /logs/verifier"
        )
        + "r2e_open_logs\n"
        "exit 0\n"
    )


def build_eval_script(
    base_commit: str,
    test_patch: str,
    test_cmds: list[str],
    *,
    language: str | None = None,
    fail_to_pass: list[str] | None = None,
    pass_to_pass: list[str] | None = None,
    run_timeout: int | None = None,
    copy_timeout: int | None = None,
    mutants: Mutants | None = None,
) -> str:
    """The `tests/test.sh` every native runtime task ships (see
    ``build_separate_eval_script``): graded in Harbor's separate verifier."""
    return build_separate_eval_script(
        base_commit,
        test_patch,
        test_cmds,
        language=language,
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
        run_timeout=run_timeout,
        copy_timeout=copy_timeout,
        mutants=mutants,
    )


def _runtime_aux_files(fail_to_pass: list[str], pass_to_pass: list[str]) -> dict[str, str]:
    """The plain task artifacts the graded test.sh reads from /tests.

    Shipping these as files (vs base64 inside test.sh) keeps task semantics
    inspectable and test.sh small. Harbor exposes tests/ at /tests in the
    container.
    """
    return {
        "tests/verifier.py": _verifier_source(),
        "tests/f2p.json": json.dumps(fail_to_pass, indent=2),
        "tests/p2p.json": json.dumps(pass_to_pass or [], indent=2),
    }


def _files_in_patch(unified_diff: str) -> list[str]:
    """Extract the unique 'b/' file paths touched by a unified diff."""
    if not unified_diff.strip():
        return []
    seen: list[str] = []
    for m in _DIFF_HEADER_RE.finditer(unified_diff):
        b = m.group(2)
        if b not in seen:
            seen.append(b)
    return seen


# Lines in a test_patch that introduce a new test function/class.
# Python: `+def test_foo(...)`, `+    def test_bar(...)`, `+class TestX`
# JS/TS:  `+it(`, `+test(`, `+describe(` (not currently filtered)
# Go:     `+func TestFoo(`
_NEW_TEST_FUNC_RE = re.compile(
    r"^\+\s*(?:def\s+test_\w+|class\s+\w*[Tt]est\w*|func\s+Test\w+|it\s*\(|test\s*\(|describe\s*\()",
)


def _diff_loc_changed(unified_diff: str) -> int:
    """Count real +/- lines in a unified diff (excludes +++/--- file markers)."""
    n = 0
    for line in (unified_diff or "").splitlines():
        if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++ ", "--- ")):
            n += 1
    return n


def _difficulty_bucket(f2p_count: int, loc_changed: int) -> str:
    """Coarse difficulty from oracle size — lets consumers slice train/eval.

    Combines the fix size (LOC) and how many distinct behaviours must be
    restored (F2P count). Mirrors Arc 1's pr_diff buckets on LOC.
    """
    if loc_changed <= 5 and f2p_count <= 1:
        return "trivial"
    if loc_changed <= 20:
        return "small"
    if loc_changed <= 80:
        return "medium"
    return "large"


def _count_new_test_funcs(test_patch: str) -> int:
    """Count new test-function definitions added in a unified diff.

    Used to filter out PRs whose test_patch is comment-only or docstring-only
    (cosmetic changes that can't produce a FAIL_TO_PASS oracle).
    """
    if not test_patch.strip():
        return 0
    return sum(1 for line in test_patch.splitlines() if _NEW_TEST_FUNC_RE.match(line))


_PYTEST_COMMAND_RE = re.compile(r"\b(?:pytest|py[.]test)\b")


def normalize_test_cmds_for_runtime(test_cmds: list[str]) -> list[str]:
    """Adapt bootstrap-recorded test commands for actual per-PR execution.

    Bootstrap prefers fast/tolerant commands (e.g. `pytest --collect-only`)
    so it can declare success without running every test. For pr_runtime,
    we need commands that *run* tests and emit per-test pass/fail lines
    that our parsers can read.

    Transforms (per runner):
      pytest / py.test:
        - Drop `--collect-only` / `--co` so pytest actually runs tests
        - Drop `-q` / `--quiet`: suppresses per-test names; cancels `-v` in pytest 9
        - Add `-v` if no verbosity flag is present
      python unittest / Django:
        - Add a verbosity flag when none is present, since these runners
          print only dots at the default level: `-v` for unittest, `-v 2`
          for `manage.py test`, and `--verbosity 2` for Django's in-repo
          `tests/runtests.py`, which takes no `-v` count.
      go test:
        - Add `-v` if missing (default `go test` doesn't print --- PASS lines)
        - Add `-count=1` if missing (no cached results)
      cargo test:
        - Default output is already parseable; no transform needed
      cargo nextest run:
        - Add `--status-level pass` (or raise a lower one) and `--color never`
      jest / npm test:
        - Add `--verbose` if not present, so per-test ✓/✕ lines are emitted
        - Some configs swallow stdout via `--silent`; we strip that
      mocha:
        - Use `--reporter spec` instead: mocha has no `--verbose` option and
          reads the next word as its value, dropping a test file appended later
      vitest:
        - Use `--reporter=verbose` instead: vitest rejects `--verbose`, and its
          default reporter collapses fully passing files to one summary line
    """
    out: list[str] = []
    for cmd in test_cmds:
        cleaned = cmd

        # Strip shell pipes / redirects / tail-truncators that bootstrap agents
        # sometimes append (e.g. `pytest -q 2>&1 | head -50`) so we capture only
        # the test runner invocation. If we keep them, `targeted_test_cmds_for_pr`
        # appends test files AFTER the pipe → broken command.
        # `[^|]*` swallows whatever flags follow `head`/`tail` (`-50`, `-n 100`, etc.)
        # without crossing into another piped command.
        cleaned = re.sub(r"\s*\|\s*(?:head|tail)\s*[^|]*$", "", cleaned)
        cleaned = re.sub(r"\s*2>&1\b", "", cleaned)
        cleaned = re.sub(r"\s*&?>\s*/dev/null\b", "", cleaned)
        cleaned = cleaned.rstrip(" |&")

        # --- pytest ---
        if _PYTEST_COMMAND_RE.search(cleaned):
            cleaned = re.sub(r"\s+--collect-only\b", "", cleaned)
            cleaned = re.sub(r"\s+--co\b", "", cleaned)  # pytest's short form
            # Strip -q/--quiet: it suppresses per-test names that the log parser needs.
            # -q and -v cancel each other in pytest 9 (verbosity counter), so -q must go.
            cleaned = re.sub(r"\s+(?:-q|--quiet)\b", "", cleaned)
            if not re.search(r"\s-v\b|\s--verbose\b|-vv\b", cleaned):
                cleaned = cleaned.rstrip() + " -v"

        # --- python unittest / Django (manage.py test, tests/runtests.py) ---
        elif re.search(r"\bunittest\b|manage\.py\s+test\b|runtests\.py", cleaned):
            if not re.search(r"\s-v\b|\s--verbose\b|\s--verbosity\b|-vv\b", cleaned):
                if re.search(r"runtests\.py", cleaned):
                    flag = " --verbosity 2"
                elif re.search(r"manage\.py\s+test\b", cleaned):
                    flag = " -v 2"  # Django's runner counts verbosity
                else:
                    flag = " -v"  # unittest's own flag
                cleaned = cleaned.rstrip() + flag

        # --- go test ---
        elif re.search(r"\bgo\s+test\b", cleaned):
            if not re.search(r"\s-v\b", cleaned):
                # Insert -v right after `go test`; positional args go after
                cleaned = re.sub(r"\bgo\s+test\b", "go test -v", cleaned, count=1)
            if not re.search(r"\s-count(?:=|\s)", cleaned):
                # Never serve a cached result: every graded run executes the tests.
                cleaned = re.sub(r"\bgo\s+test\b", "go test -count=1", cleaned, count=1)

        # --- cargo nextest ---
        elif re.search(r"\bcargo\s+nextest\s+run\b", cleaned):
            # A repo's nextest config can lower `status-level` and hide the
            # PASS lines that FAIL_TO_PASS needs, so ask for them explicitly.
            # Flags go right after `run`: anything after `--` goes to the tests.
            cleaned = re.sub(
                r"\s+--status-level(?:=|\s+)(?:none|fail|retry|slow|leak)\b", "", cleaned
            )
            extra = ""
            if not re.search(r"\s--status-level\b", cleaned):
                extra += " --status-level pass"
            if not re.search(r"\s--color\b", cleaned):
                extra += " --color never"
            cleaned = re.sub(r"\bcargo\s+nextest\s+run\b", r"\g<0>" + extra, cleaned, count=1)

        # --- cargo test ---
        elif re.search(r"\bcargo\s+test\b", cleaned):
            # `cargo test` already prints `test NAME ... ok/FAILED/ignored`
            # by default — no transformation needed. If a user passed
            # `-q`, the per-test lines disappear; strip it.
            cleaned = re.sub(r"\s+(?:-q|--quiet)\b", "", cleaned)

        # --- jest / npm test / yarn test / pnpm test ---
        elif re.search(r"\b(?:jest|mocha|vitest|npm\s+test|yarn\s+test|pnpm\s+test)\b", cleaned):
            cleaned = re.sub(r"\s+--silent\b", "", cleaned)
            # Add a per-test reporter if the cmd is the runner itself (skip
            # wrappers where flags need to go after `--`)
            if re.search(r"\bvitest\b", cleaned):
                # `vitest --verbose` exits with `CACError: Unknown option`.
                cleaned = re.sub(r"\s+--verbose\b", "", cleaned)
                if not re.search(r"\s--reporter\b", cleaned):
                    cleaned = cleaned.rstrip() + " --reporter=verbose"
            elif re.search(r"\bmocha\b", cleaned):
                # mocha has no `--verbose`: it reads the next word as the flag's
                # value, which swallows a test file appended after it.
                cleaned = re.sub(r"\s+--verbose\b", "", cleaned)
                if not re.search(r"\s(?:--reporter|-R)\b", cleaned):
                    cleaned = cleaned.rstrip() + " --reporter spec"
            elif re.search(r"\bjest\b", cleaned) and not re.search(
                r"\s--verbose\b|\s--reporter\b", cleaned
            ):
                cleaned = cleaned.rstrip() + " --verbose"

        out.append(cleaned.strip())
    return out


# Pytest-style file extensions we know how to target. Anything else triggers
# the fallback to "run the whole suite".
_PYTEST_TARGETABLE_EXT = (".py",)
_JEST_TARGETABLE_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


def _go_packages_from_test_files(test_files: list[str]) -> list[str]:
    """Map `pkg/foo/bar_test.go` → `./pkg/foo` for Go's package-path CLI."""
    pkgs: list[str] = []
    for f in test_files:
        if not f.endswith("_test.go"):
            continue
        # Directory of the test file, prefixed with ./ for `go test` package syntax
        parts = f.rsplit("/", 1)
        pkg = "./" + parts[0] if len(parts) == 2 else "./"
        if pkg not in pkgs:
            pkgs.append(pkg)
    return pkgs


def targeted_test_cmds_for_pr(test_cmds: list[str], test_files: list[str]) -> list[str]:
    """Limit the test invocation to the file paths the PR's test_patch touches.

    Running the whole suite on every PR is 10-50× slower than running only
    the files the PR cares about. SWE-bench-Live's harness does the same.

    Per-runner rules:
      pytest / py.test: append the changed .py test files as positional args
      jest:    append the changed .js/.ts test files as positional args
      go test: replace `./...` (or trailing nothing) with the package
               directories containing changed `*_test.go` files
      cargo:   no targeting — Rust's filter is name-substring, not file
               (we'd need to introspect test names; whole-suite is fine)

    Skips if the cmd already has a positional path arg.
    """
    if not test_files:
        return test_cmds

    py_files = [f for f in test_files if f.endswith(_PYTEST_TARGETABLE_EXT)]
    js_files = [f for f in test_files if f.endswith(_JEST_TARGETABLE_EXT)]
    go_pkgs = _go_packages_from_test_files(test_files)

    out: list[str] = []
    for cmd in test_cmds:
        # --- pytest ---
        if _PYTEST_COMMAND_RE.search(cmd) and py_files:
            tokens = cmd.split()
            pytest_idx = next(
                (
                    i
                    for i, t in enumerate(tokens)
                    if t in {"pytest", "py.test"} or t.endswith(("/pytest", "/py.test"))
                ),
                -1,
            )
            if pytest_idx >= 0:
                tail = tokens[pytest_idx + 1 :]
                has_path_arg = any(
                    not t.startswith("-") and (t.endswith(".py") or "/" in t) for t in tail
                )
                if not has_path_arg:
                    cmd = cmd.rstrip() + " " + " ".join(py_files)

        # --- go test ---
        elif re.search(r"\bgo\s+test\b", cmd) and go_pkgs:
            # Replace `./...` with the targeted packages; if neither is present,
            # append the packages
            if "./..." in cmd:
                cmd = cmd.replace("./...", " ".join(go_pkgs))
            elif not re.search(r"\b\./\S+\b", cmd):
                cmd = cmd.rstrip() + " " + " ".join(go_pkgs)

        # --- jest / npx jest / mocha / vitest ---
        elif re.search(r"\b(?:jest|mocha|vitest)\b", cmd) and js_files:
            tokens = cmd.split()
            # If a positional file path is already present, don't double-up
            has_path = any(
                not t.startswith("-") and (t.endswith(_JEST_TARGETABLE_EXT) or "/" in t)
                for t in tokens[1:]
            )
            if not has_path:
                cmd = cmd.rstrip() + " " + " ".join(js_files)

        out.append(cmd)
    return out


class PRRuntimePipeline:
    """Sandbox-verified PR mining. Implements the `Pipeline` Protocol."""

    name: ClassVar[PipelineName] = PipelineName.PR_RUNTIME
    required_capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.PULL_REQUESTS})
    requires_bootstrap: ClassVar[bool] = True
    experimental: ClassVar[bool] = False  # stable

    def __init__(
        self,
        input: GenerationInput,
        options: PRRuntimeOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "pr_runtime requires a BootstrapResult (set requires_bootstrap=True "
                "and let cmd_generate trigger it, or pass one explicitly)"
            )
        self.input = input
        self.options = options
        self.bootstrap = bootstrap
        self._progress_cb = None

    def set_progress_callback(self, cb) -> None:
        self._progress_cb = cb

    def _emit_progress(self, name: str, outcome: str, reason: str = "") -> None:
        if self._progress_cb is not None:
            try:
                self._progress_cb(name=name, outcome=outcome, reason=reason)
            except Exception as exc:
                logger.debug("progress callback failed: %s", exc)

    # ----- run loop -----------------------------------------------------------

    def run(self, out_dir: Path) -> PipelineResult:
        out_dir.mkdir(parents=True, exist_ok=True)

        token = resolve_repo_token(self.input.repo, self.input.auth)
        self._token = token  # reused by _build_task to fetch linked-issue text
        self._provider = provider_for(self.input.repo)  # GitHub vs GitLab dispatch
        if self.input.repo.access == "private" and not token:
            raise RuntimeError(
                "private repo specified but no token resolved. "
                "Run `gh auth login` / set GITHUB_TOKEN (GitHub) or GITLAB_TOKEN (GitLab)."
            )

        owner, name = self.input.repo.owner_name
        logger.info("listing merged PRs for %s/%s (limit=%d)", owner, name, self.options.limit)
        try:
            prs = self._provider.list_merged_prs(
                owner,
                name,
                limit=self.options.limit,
                since=self.options.since,
                until=self.options.until,
                skip_drafts=self.options.skip_drafts,
                token=token,
            )
        except _PROVIDER_ERRORS as exc:
            raise RuntimeError(f"failed to list PRs: {exc}") from exc

        skip_reasons: dict[str, int] = {}
        emitted = 0
        sandbox = None  # lazy-init for the validation loop

        try:
            for pr in prs:
                pr_label = f"{owner}/{name}#{pr.number}"

                # Pre-validation skip filters
                reason = self._pre_filter(pr)
                if reason:
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    self._emit_progress(pr_label, "skip", reason)
                    continue

                # Fetch diff
                try:
                    diff = self._provider.fetch_pr_diff(owner, name, pr.number, token=token)
                except _PROVIDER_ERRORS as exc:
                    logger.warning("PR #%d: diff fetch failed: %s", pr.number, exc)
                    skip_reasons["diff_fetch_failed"] = skip_reasons.get("diff_fetch_failed", 0) + 1
                    self._emit_progress(pr_label, "error", "diff_fetch_failed")
                    continue

                patch, test_patch = split_patch_and_test_patch(diff)
                if not patch.strip():
                    skip_reasons["empty_source_patch"] = (
                        skip_reasons.get("empty_source_patch", 0) + 1
                    )
                    self._emit_progress(pr_label, "skip", "empty_source_patch")
                    continue
                if not test_patch.strip():
                    skip_reasons["no_test_patch"] = skip_reasons.get("no_test_patch", 0) + 1
                    self._emit_progress(pr_label, "skip", "no_test_patch")
                    continue

                # Structural quality filters (cheap, run before validation):
                # - CI-only PRs: source patch is 100% under .github/
                # - No new test functions: test_patch only edits comments/docstrings
                structural_reason = self._structural_quality_filter(patch, test_patch)
                if structural_reason:
                    skip_reasons[structural_reason] = skip_reasons.get(structural_reason, 0) + 1
                    self._emit_progress(pr_label, "skip", structural_reason)
                    continue

                # Lite-style structural filters
                lite_reason = self._lite_filter(pr, patch)
                if lite_reason:
                    skip_reasons[lite_reason] = skip_reasons.get(lite_reason, 0) + 1
                    self._emit_progress(pr_label, "skip", lite_reason)
                    continue

                # Validation (optional via skip_validation)
                fail_to_pass: list[str] = []
                pass_to_pass: list[str] = []
                timing: GradingTime | None = None
                mutants: Mutants | None = None
                validated_cmds: list[str] = []
                flaky_dropped = 0
                validation_status = "skipped"
                if not self.options.skip_validation:
                    if sandbox is None:
                        sandbox = self._start_validation_sandbox()
                    from repo2rlenv.pipelines.pr_runtime_validate import validate_pr

                    targeted_cmds = targeted_test_cmds_for_pr(
                        normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
                        _files_in_patch(test_patch),
                    )
                    outcome = validate_pr(
                        sandbox=sandbox,
                        base_commit=pr.base_sha,
                        patch=patch,
                        test_patch=test_patch,
                        test_cmds=targeted_cmds,
                        language=self.bootstrap.language.value,
                        timeout=self.options.validation_timeout_sec,
                    )
                    fail_to_pass = outcome.fail_to_pass
                    pass_to_pass = outcome.pass_to_pass
                    timing = outcome.timing
                    mutants = outcome.mutants
                    validated_cmds = outcome.test_cmds
                    flaky_dropped = len(outcome.flaky)
                    validation_status = outcome.status
                    if timing is not None and timing.over_cap:
                        reason = "verifier_timeout_over_cap"
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                        self._emit_progress(pr_label, "skip", outcome.reason or reason)
                        continue
                    if (
                        self.options.require_fail_to_pass
                        and len(fail_to_pass) < self.options.min_fail_to_pass
                    ):
                        skip_reasons["no_fail_to_pass"] = skip_reasons.get("no_fail_to_pass", 0) + 1
                        self._emit_progress(pr_label, "skip", outcome.reason or "no_fail_to_pass")
                        continue

                # Emit the Harbor task
                task = self._build_task(
                    pr,
                    patch,
                    test_patch,
                    fail_to_pass=fail_to_pass,
                    pass_to_pass=pass_to_pass,
                    validation_status=validation_status,
                    flaky_dropped=flaky_dropped,
                    timing=timing,
                    mutants=mutants,
                    test_cmds=validated_cmds or None,
                )
                write_harbor_task(task, out_dir)
                emitted += 1
                logger.info(
                    "emitted task %s (F2P=%d, P2P=%d)",
                    task.name,
                    len(fail_to_pass),
                    len(pass_to_pass),
                )
                self._emit_progress(task.name, "emit")
        finally:
            if sandbox is not None:
                sandbox.cleanup()

        return PipelineResult(
            candidates=len(prs),
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # ----- filters ------------------------------------------------------------

    def _pre_filter(self, pr: PullRequestSummary) -> str | None:
        """Cheap filters that don't need the diff."""
        if pr.is_draft and self.options.skip_drafts:
            return "draft"
        if not pr.merged_at:
            return "not_merged"
        if not pr.changed_files:
            return "no_files"
        if _is_non_bug_pr(pr.title):
            # Backports / cherry-picks / release chores / reverts / version
            # bumps aren't real fix tasks, and their bodies leak commit SHAs
            # and fix-PR links. Drop them up front.
            return "non_bug_pr"
        if self.options.require_linked_issue and _linked_issue_number(pr.body or "") is None:
            return "no_linked_issue"
        if (
            self.options.min_problem_statement_words > 0
            and _word_count(pr.body or "") < self.options.min_problem_statement_words
        ):
            return "problem_statement_too_short"
        return None

    def _structural_quality_filter(self, source_patch: str, test_patch: str) -> str | None:
        """Cheap diff-level filters that catch over-emitted task types.

        Returns a skip reason string, or None to keep.

        Two filters here, both shipping a lot of false positives in v0.3:
          1. CI-only PRs: source patch is 100% under .github/. These are
             tooling changes (zizmor scan, publish workflows) — no real
             code-fix signal.
          2. No new test functions: test_patch only edits comments /
             docstrings (e.g. typo cleanup PRs). Without a new test that
             FAILS at base_commit, there can be no FAIL_TO_PASS oracle.
        """
        source_files = _files_in_patch(source_patch)
        # Filter 1: CI-only — all source files are workflow YAMLs
        if (
            self.options.skip_ci_only
            and source_files
            and all(p.startswith(".github/") for p in source_files)
        ):
            return "ci_only_patch"
        # Filter 2: test_patch must add ≥1 new test function
        if self.options.require_new_test_funcs:
            n_new = _count_new_test_funcs(test_patch)
            if n_new < 1:
                return "no_new_test_funcs"
        return None

    def _lite_filter(self, pr: PullRequestSummary, source_patch: str) -> str | None:
        """SWE-bench Lite-style structural filters."""
        source_files = _files_in_patch(source_patch)
        if len(source_files) > self.options.max_source_files_per_pr:
            return "too_many_source_files"
        if self.options.lite_filter:
            if len(source_files) != 1:
                return "lite_not_single_source_file"
            if _word_count(pr.body or "") < 40:
                return "lite_problem_too_short"
            # Reject if PR body contains images / external links / cross-PR/issue refs
            body = (pr.body or "").lower()
            if re.search(r"!\[[^\]]*\]\(|<img\s", body):
                return "lite_has_image"
            if re.search(r"\bhttps?://(?!github\.com/)", body):
                return "lite_has_external_link"
            if re.search(r"\b[a-f0-9]{7,40}\b", body):
                return "lite_has_commit_sha"
        return None

    # ----- sandbox -----------------------------------------------------------

    def _start_validation_sandbox(self):
        """Spin up a DockerSandbox from the bootstrap image (shared across PRs)."""
        # The bootstrap image already contains the repo at the bootstrap-time HEAD.
        # We pass repo_dir=None? No — DockerSandbox.start requires a repo_dir to copy in.
        # The repo is already in the image; we'll just `git checkout` to each PR's base_commit
        # from inside the container, so the path here is a no-op marker.
        import tempfile

        from repo2rlenv.bootstrap.docker import DockerSandbox

        marker = Path(tempfile.mkdtemp(prefix="r2e-pr-runtime-"))
        (marker / ".keep").write_text("")  # docker cp <src>/. <dst> works on any non-empty dir
        # Pull just the tag, don't re-copy the repo (image already has it)
        sandbox = DockerSandbox.start(
            base_image=self.bootstrap.image_tag,
            repo_dir=marker,
            platform=self.input.bootstrap.platform,
        )
        return sandbox

    # ----- task builder -------------------------------------------------------

    def _build_task(
        self,
        pr: PullRequestSummary,
        patch: str,
        test_patch: str,
        *,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
        validation_status: str,
        flaky_dropped: int = 0,
        timing: GradingTime | None = None,
        mutants: Mutants | None = None,
        test_cmds: list[str] | None = None,
    ) -> HarborTask:
        owner, name = self.input.repo.owner_name
        task_id = f"{owner}__{name}-{pr.number}"

        # Grade with exactly what validation ran (Node script indirection resolved).
        resolved_test_cmds = test_cmds or targeted_test_cmds_for_pr(
            normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
            _files_in_patch(test_patch),
        )
        eval_script = build_eval_script(
            base_commit=pr.base_sha,
            test_patch=test_patch,
            test_cmds=resolved_test_cmds,
            language=self.bootstrap.language.value,
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            run_timeout=timing.run_timeout if timing is not None else None,
            copy_timeout=timing.copy_timeout if timing is not None else None,
            mutants=mutants,
        )
        # Use image_tag for local-only bootstraps (Docker can resolve locally),
        # image_digest only when the image is in a registry that BuildKit can
        # actually pull from.
        image_ref = (
            self.bootstrap.image_digest
            if self.bootstrap.pushed_to_registry
            else self.bootstrap.image_tag
        )
        dockerfile = build_environment_dockerfile(
            bootstrap_image=image_ref,
            base_commit=pr.base_sha,
        )

        repo2env = {
            "pipeline": "pr_runtime",
            "pipeline_version": "0.3.0",
            "repo": f"{owner}/{name}",
            "ref": pr.base_sha,
            "reference": pr.url,
            "source_access": self.input.repo.access,
            "built_at": datetime.now(UTC).isoformat(),
            **({"synthesis_llm": self.input.llm.qualified_name} if self.input.llm else {}),
            "reward_kinds": ["test_execution", "diff_similarity"],
            "pr_runtime": {
                "pr_url": pr.url,
                "pr_merged_at": pr.merged_at,
                "base_commit": pr.base_sha,
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "validation_status": validation_status,
                # Tests dropped at validation because their status varied across reruns.
                "flaky_dropped": flaky_dropped,
                "bootstrap_image": self.bootstrap.image_digest,
                # Graded reward: reward.txt carries f2p_rate*p2p_rate (training
                # signal); the strict SWE-bench `resolved` bool + full breakdown
                # go to reward-details.json (eval signal — Harbor's reward.json
                # schema is flat-numeric-only). See _pr_runtime_verifier.py.
                "reward_mode": "graded",
                "harbor_min_version": MIN_HARBOR_VERSION,
                # Measured at validation; sets the per-run and verifier limits.
                **({"grading_time": timing.metadata()} if timing is not None else {}),
                "tamper_check": "mutants" if mutants is not None and mutants.ids else "unavailable",
            },
            # Difficulty + coverage metadata so consumers can slice train/eval
            # by hardness and judge regression-guard strength (p2p_count == 0
            # means no P2P regression guard — weaker eval; see UTBoost).
            "reward_calibration": {
                "f2p_count": len(fail_to_pass),
                "p2p_count": len(pass_to_pass),
                "source_files": len(_files_in_patch(patch)),
                "loc_changed": _diff_loc_changed(patch),
                "difficulty": _difficulty_bucket(len(fail_to_pass), _diff_loc_changed(patch)),
            },
        }

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=pr.title or task_id,
            instruction=_build_instruction(
                pr,
                owner,
                name,
                token=getattr(self, "_token", None),
                provider=getattr(self, "_provider", github),
            ),
            oracle_diff=patch,
            repo2env=repo2env,
            difficulty="medium",
            category="bugfix",
            keywords=[name, "pr_runtime"],
            environment_dockerfile=dockerfile,
            test_script=eval_script,
            verifier=verifier_config(timing),
            artifacts=WORKSPACE_ARTIFACT,
            # Ship verifier.py + f2p.json + p2p.json as plain, inspectable task
            # artifacts that test.sh reads from /tests (no base64 in test.sh).
            # The egress guard blackholes the hosts that serve this PR's merged
            # diff so the agent cannot fetch the gold patch at run time.
            aux_files={
                **(_runtime_aux_files(fail_to_pass, pass_to_pass) if fail_to_pass else {}),
                **separate_verifier_files(mutants),
                "environment/docker-compose.yaml": egress_guard_compose(
                    fix_source_hosts(self.input.repo.source_kind, self.bootstrap.language),
                    self.bootstrap.language,
                ),
            },
        )
