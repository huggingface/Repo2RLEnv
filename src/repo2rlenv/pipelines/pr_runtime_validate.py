"""Validate a candidate PR inside the bootstrap container.

Computes the FAIL_TO_PASS + PASS_TO_PASS sets that SWE-bench-style oracles
need. Workflow inside one container:

  1. Reset working tree to base_commit (`git reset --hard`)
  2. Apply test_patch only           → run tests → pre_status (per-test)
  3. Reset, then apply patch + test_patch → run tests → post_status, three
     times; a test whose status differs between those runs is flaky and is
     dropped (the SWE-rebench V2 / R2E-Gym convention)
  4. F2P = tests that FAILED in (2) and PASS in every run of (3)
     P2P = tests that PASSED in (2) and in every run of (3)
  5. Rerun (3) once with mutants of a few tracked tests; keep mutants that
     fail there while every other tracked status matches (3) (see `_mutants`)

Every stage grades the way the separate verifier does: the patched tree goes
through `_grader_tools.py rebuild` against a copy of the base tree (so the gold
patch crosses exactly as a submission would), then the same runner
configuration guard, then `_grader_launcher.py` runs the tests as an
unprivileged user. A suite that only passes as root, or a fix that touches a
path the grader drops, fails here instead of shipping as a task nobody can
solve.

We mirror SWE-bench's harness/test_spec/utils.py:make_eval_script_list_common
for the per-stage script, and harness/grading.py:get_logs_eval for the
status-extraction direction. Implementation is independent (no swebench
import); see references/SWE-bench/ for the reference code.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from repo2rlenv.bootstrap.docker import DockerSandbox
from repo2rlenv.log_parsers import parse_logs
from repo2rlenv.pipelines._eval_script import upload
from repo2rlenv.pipelines._grading_time import GradingTime, fit_runs, measure
from repo2rlenv.pipelines._mutants import Mutants, make_mutants, validated
from repo2rlenv.pipelines._mutants_ts import language_of, make_tree_sitter_mutants
from repo2rlenv.pipelines._node_runner import freeze_node_commands
from repo2rlenv.pipelines.pr_runtime import (
    _PYTEST_COMMAND_RE,
    _PYTHONS,
    _access_flags,
    _child_path,
    _files_in_patch,
    _guard_env,
    _test_config_reset,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ValidationOutcome:
    status: str  # verified | partial | failed | skipped
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    pre_log: str = ""  # raw test output, pre-fix (truncated)
    post_log: str = ""  # raw test output, post-fix
    reason: str = ""  # populated on status != "verified"
    flaky: list[str] = field(default_factory=list)  # dropped: status varied across post-fix runs
    # Slowest measured test run and non-test overhead, for the grading time limits.
    timing: GradingTime | None = None
    # The test commands as validated (Node script indirection resolved); grade with these.
    test_cmds: list[str] = field(default_factory=list)
    mutants: Mutants | None = None  # validated mutant tests, if any


_HEREDOC = "EOF_R2E_VALIDATE"
# Post-fix runs per candidate. A test must keep its status in all of them to be
# tracked, so a flaky test can't flip an honest submission's reward.
POST_FIX_RUNS = 3
# Root-only directory for the launcher's log (it refuses one the test user can
# read) and for the copy of the base tree the rebuild diffs against.
_LOG_DIR = "/opt/r2e-validate"
_REBUILD_RE = re.compile(r"^R2E_REBUILD_STATUS=(\S*)$", re.MULTILINE)
_SECONDS_RE = re.compile(r"^R2E_TEST_SECONDS=([0-9.]+)$", re.MULTILINE)
_COPY_RE = re.compile(r"^R2E_COPY_SECONDS=([0-9.]+)$", re.MULTILINE)
# The launcher refused to run: the image lets the test user write a path root
# later imports or executes from. No submission can be graded safely there.
_UNSAFE_IMAGE = "R2E_TEST_EXIT_CODE=unsafe_image"
_EXIT_RE = re.compile(r"^R2E_TEST_EXIT_CODE=(\d+)$", re.MULTILINE)


class _Stopwatch:
    """(test seconds, whole stage seconds) for each validation stage that ran tests."""

    def __init__(self) -> None:
        self.samples: list[tuple[float, float]] = []
        # The rebuild's copy of the image's repository, timed for its limit.
        self.copies: list[float] = []

    def run(self, sandbox: DockerSandbox, script: str, timeout: int):
        started = time.monotonic()
        result = sandbox.exec(script, timeout=timeout)
        stage = time.monotonic() - started
        found = _SECONDS_RE.search(result.stdout or "")
        if found:
            self.samples.append((float(found.group(1)), stage))
        copied = _COPY_RE.search(result.stdout or "")
        if copied:
            self.copies.append(float(copied.group(1)))
        return result


def _status(found: re.Match | None) -> str:
    return (found.group(1) or "grader error") if found else "not run"


def _heredoc_apply(patch: str) -> str:
    return f"git apply --verbose --reject - <<'{_HEREDOC}'\n{patch}\n{_HEREDOC}"


# Keys under which a stage uploads its patches; anything else is a file path.
_PATCH, _TEST_PATCH, _MUTANTS = "\0patch", "\0test_patch", "\0mutants"


def _apply(patch: str, staged: dict[str, str], key: str) -> str:
    """Apply a staged patch (uploaded by `_stage`), or inline it when nothing was staged."""
    if key in staged:
        return f"base64 -d < {staged[key]} | git apply --verbose --reject -"
    return _heredoc_apply(patch)


def _stage(
    sandbox: DockerSandbox,
    *,
    patch: str | None = None,
    test_patch: str | None = None,
    mutants: Mutants | None = None,
) -> dict[str, str]:
    """Upload a stage's patches (and mutant spec) first: a script carrying them
    in argv breaks once they pass Linux's 128 KiB per-argument cap."""
    contents: dict[str, str] = {}
    if mutants is not None:
        contents[_MUTANTS] = mutants.spec()
    # The heredoc this replaces always ended the patch with a newline; keep that.
    if patch and patch.strip():
        contents[_PATCH] = patch if patch.endswith("\n") else patch + "\n"
    if test_patch and test_patch.strip():
        contents[_TEST_PATCH] = test_patch if test_patch.endswith("\n") else test_patch + "\n"
    return upload(sandbox, contents)


def _test_block(test_cmds: list[str]) -> str:
    """The one `bash -c` script the launcher runs: commands share one shell (`&&`)."""
    return " && ".join(test_cmds) if test_cmds else "echo 'no test_cmds'"


def _build_stage_script(
    base_commit: str,
    *,
    apply_patch: str | None,
    apply_test_patch: str | None,
    test_cmds: list[str],
    language: str | None = None,
    staged: dict[str, str] | None = None,
    keep_collecting: bool = False,
) -> str:
    """Build the shell script for one validation stage (pre-fix or post-fix).

    Always resets to base_commit first, then applies whichever patches the
    caller supplies (None ⇒ skip), then runs the test commands wrapped in
    START/END markers so the parser knows where output starts.

    `keep_collecting` lets pytest run the remaining test files when one of them
    fails to import (the pre-fix stage, where a test may import what the fix adds).

    Assumes the caller has already ensured `base_commit` is fetchable in the
    container's git object database (see `_fetch_base_commit`). The bootstrap
    image only ships the shallow-clone HEAD, so historical PR base commits
    aren't present until we fetch them explicitly.
    """
    parts: list[str] = [
        "set -uxo pipefail",
        "cd /workspace",
        "git config --global --add safe.directory /workspace",
        f"git reset --hard {base_commit}",
        # Keep language dep/build dirs (node_modules, target, vendor, ...) so
        # the clean doesn't wipe deps the suite needs. See _GIT_CLEAN_EXCLUDES.
        "git clean -fdx -e .venv -e venv -e __pycache__ -e .tox "
        "-e node_modules -e target -e vendor -e .gradle -e .next -e .pytest_cache || true",
    ]
    candidates = " ".join(_PYTHONS)
    parts.append(
        f'R2E_PY=""; for p in {candidates}; do [ -x "$p" ] && {{ R2E_PY="$p"; break; }}; done; '
        '[ -n "$R2E_PY" ] || exit 1'
    )
    staged = staged or {}
    here = Path(__file__).parent
    tools = (here / "_grader_tools.py").read_text(encoding="utf-8")
    launcher = (here / "_grader_launcher.py").read_text(encoding="utf-8")
    parts.append("set +x")
    parts.append(f"rm -rf {_LOG_DIR} && mkdir -m 700 {_LOG_DIR}")
    parts.append(
        f"cat > {_LOG_DIR}/grader_tools.py <<'R2E_GRADER_TOOLS'\n{tools}\nR2E_GRADER_TOOLS"
    )
    parts.append(
        f"cat > {_LOG_DIR}/launcher.py <<'R2E_GRADER_LAUNCHER'\n{launcher}\nR2E_GRADER_LAUNCHER"
    )
    parts.append("set -x")
    # The base tree as the image ships it, then the fix as an agent would leave
    # it, then the grader's rebuild of one from the other.
    parts.append(f"cp -a /workspace {_LOG_DIR}/pristine")
    if apply_patch and apply_patch.strip():
        parts.append(_apply(apply_patch, staged, _PATCH))
    parts.append(
        f'R2E_SUBMISSION="$("$R2E_PY" -B -I -S {_LOG_DIR}/grader_tools.py rebuild --agent /workspace '
        f"--pristine {_LOG_DIR}/pristine --patch-out {_LOG_DIR}/submission.patch "
        f'--report-out {_LOG_DIR}/submission.json --aside-dir {_LOG_DIR}/aside)"'
    )
    parts.append('echo "R2E_REBUILD_STATUS=${R2E_SUBMISSION}"')
    parts.append('[ "$R2E_SUBMISSION" = ok ] || exit 1')
    parts.append(
        'echo "R2E_COPY_SECONDS=$("$R2E_PY" -B -I -S -c '
        '\'import json,sys; print(json.load(open(sys.argv[1])).get("copy_seconds", ""))\' '
        f'{_LOG_DIR}/submission.json)"'
    )
    # Unlike the verifier, validation keeps using this container.
    parts.append(f"rm -rf {_LOG_DIR}/pristine {_LOG_DIR}/aside")
    parts.append("cd /workspace")
    # Same config reset as test.sh, so F2P/P2P are measured the way the task
    # is graded (a gold patch's own conftest.py or config change doesn't count).
    guard = _test_config_reset(base_commit, '"$R2E_PY" -B -I -S', '"$R2E_GUARD_ENV"')
    parts.append('R2E_GUARD_ENV="$(mktemp)"')
    parts.append(f'{guard}\nR2E_CONFIG_RC=$?\n[ "$R2E_CONFIG_RC" -eq 0 ] || exit 1')
    parts.append(_guard_env().rstrip("\n"))
    if apply_test_patch and apply_test_patch.strip():
        parts.append(_apply(apply_test_patch, staged, _TEST_PATCH))
    if apply_test_patch and apply_test_patch.strip():
        # The hidden test files are rewritten with one fresh mtime the way every
        # graded run does it (run B with the mutants swapped in), so a suite an
        # mtime-based build tool would then misbuild fails here, not at grading.
        paths = " ".join(f"--path {shlex.quote(p)}" for p in _files_in_patch(apply_test_patch))
        spec = ""
        if _MUTANTS in staged:
            parts.append(f"base64 -d < {staged[_MUTANTS]} > {_LOG_DIR}/mutants.json")
            spec = f" --spec {_LOG_DIR}/mutants.json --mutated"
        parts.append(
            f'"$R2E_PY" -B -I -S {_LOG_DIR}/grader_tools.py settle --root /workspace {paths}'
            f"{spec} >/dev/null || exit 1"
        )
    # The launcher runs the joined test commands as one bash invocation, as the
    # unprivileged grader user, with STDERR folded into its log: unittest's
    # TextTestRunner and jest write every result line to STDERR. The log is
    # echoed between markers on STDOUT (not `: 'MARKER'`, which `set -x`
    # traces to STDERR) so `_slice_test_output` finds the test section, and
    # `set +x` keeps bash's own trace out of it.
    test_block = _test_block(test_cmds)
    writable = _access_flags(language)
    log = f"{_LOG_DIR}/test_output.log"
    if keep_collecting and any(_PYTEST_COMMAND_RE.search(cmd) for cmd in test_cmds):
        parts.append(
            'export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:+$PYTEST_ADDOPTS }'
            '--continue-on-collection-errors"'
        )
    parts.append(_child_path(language).rstrip("\n"))
    parts.append("set +x")
    parts.append(
        f'R2E_TEST_EXIT="$("$R2E_PY" -B -I -S {_LOG_DIR}/launcher.py --log {log} --cwd /workspace '
        f'--path "$R2E_CHILD_PATH" {writable} --seconds-out {_LOG_DIR}/seconds '
        # Later stages run in this container: delete what the tests left.
        f'--purge -- {shlex.quote(test_block)})"'
    )
    parts.append("echo R2E_START_TEST_OUTPUT")
    parts.append(f"cat {log}")
    parts.append("echo R2E_END_TEST_OUTPUT")
    parts.append('echo "R2E_TEST_EXIT_CODE=${R2E_TEST_EXIT}"')
    parts.append(f'echo "R2E_TEST_SECONDS=$(cat {_LOG_DIR}/seconds 2>/dev/null)"')
    parts.append("set -x")
    return "\n".join(parts)


# Install git AND ca-certificates. Minimal images (esp. node:alpine) ship
# without CA certs, so `git fetch` over HTTPS dies with "server certificate
# verification failed. CAfile: none" — which silently breaks base_commit
# fetching during validation. Install both; refresh the CA store on alpine.
_GIT_DEFENSIVE_INSTALL = (
    "(command -v git >/dev/null 2>&1 && [ -e /etc/ssl/certs/ca-certificates.crt ]) || "
    "(apt-get update >/dev/null 2>&1 && "
    "apt-get install -y --no-install-recommends git ca-certificates >/dev/null 2>&1 && "
    "rm -rf /var/lib/apt/lists/*) || "
    "(apk add --no-cache git ca-certificates >/dev/null 2>&1 && "
    "update-ca-certificates >/dev/null 2>&1) || true"
)


def _ensure_git(sandbox: DockerSandbox) -> bool:
    """Install git in the sandbox if missing + mark the repo as safe.directory.

    Idempotent — no-op when git is already on PATH. Returns True if git is
    available after the call. Tries apt-get (Debian/Ubuntu) then apk (Alpine).
    Always marks /workspace as a safe directory afterward because Docker copies
    the repo in as root-owned, which triggers git's `detected dubious ownership`
    refusal otherwise.
    """
    check = sandbox.exec("command -v git >/dev/null 2>&1 && echo OK", timeout=10)
    if not (check.ok and "OK" in check.stdout):
        sandbox.exec(_GIT_DEFENSIVE_INSTALL, timeout=120)
        re = sandbox.exec("command -v git >/dev/null 2>&1 && echo OK", timeout=10)
        if not (re.ok and "OK" in re.stdout):
            return False
    sandbox.exec("git config --global --add safe.directory /workspace", timeout=10)
    return True


def _ensure_python(sandbox: DockerSandbox) -> bool:
    """Make sure a root-owned python3 exists at a fixed path for the launcher.

    The task's environment Dockerfile installs one the same way
    (`pr_runtime.ensure_system_python`), so grading finds it too.
    """
    check = " || ".join(f"[ -x {path} ]" for path in _PYTHONS)
    if sandbox.exec(f"({check}) && echo OK", timeout=10).stdout.strip().endswith("OK"):
        return True
    sandbox.exec(
        "(apt-get update >/dev/null 2>&1 && apt-get install -y --no-install-recommends "
        "python3 >/dev/null 2>&1 && rm -rf /var/lib/apt/lists/*) || "
        "apk add --no-cache python3 >/dev/null 2>&1 || true",
        timeout=300,
    )
    return sandbox.exec(f"({check}) && echo OK", timeout=10).stdout.strip().endswith("OK")


def _fetch_base_commit(sandbox: DockerSandbox, base_commit: str, *, timeout: int = 120) -> bool:
    """Make `base_commit` available in the container's git object db.

    Bootstrap shallow-clones at depth=1, so only the bootstrap-time HEAD is
    present. Without this fetch, `git reset --hard <historical_sha>` would
    fail silently or reset against the wrong tree, masking validation bugs.

    Strategy:
      0. Ensure git is installed (Python-slim bootstrap images often skip it).
      1. If the commit already exists locally (e.g. it IS bootstrap HEAD or
         we fetched it on a prior PR in this sandbox), skip — fast path.
      2. Else `git fetch --depth 1 origin <sha>` to pull just that one commit.
         Falls back to deepening the shallow clone if the server refuses
         a by-sha fetch.

    Returns True if base_commit is now reachable, False otherwise.
    """
    if not _ensure_git(sandbox):
        logger.warning("validate_pr: could not install git in sandbox; skipping fetch")
        return False
    # Fast path: already present
    have = sandbox.exec(
        f"git -C /workspace cat-file -e {base_commit} 2>/dev/null && echo OK",
        timeout=15,
    )
    if have.ok and "OK" in have.stdout:
        return True

    # Try direct by-sha fetch (works on GitHub since 2017's uploadpack.allowAnySHA1InWant)
    r = sandbox.exec(
        f"cd /workspace && git fetch --depth 1 origin {base_commit}",
        timeout=timeout,
    )
    if r.ok:
        return True
    logger.warning(
        "validate_pr: direct fetch of %s failed (%s); deepening clone",
        base_commit[:12],
        r.stderr.strip()[:200] if r.stderr else "",
    )
    # Fallback: unshallow. Slower but always works.
    r = sandbox.exec(
        "cd /workspace && git fetch --unshallow origin || git fetch --depth 1000 origin",
        timeout=timeout * 2,
    )
    if not r.ok:
        return False
    # Re-check
    have = sandbox.exec(
        f"git -C /workspace cat-file -e {base_commit} 2>/dev/null && echo OK",
        timeout=15,
    )
    return have.ok and "OK" in have.stdout


def _slice_test_output(output: str) -> str:
    """Trim to just the test-runner section between the START/END markers.

    The markers are echoed to stdout (see `_build_stage_script`); since
    `truncated()` puts stdout before stderr, `find` returns the stdout
    occurrence first — i.e. the real test section, not the `set -x` trace.
    """
    start = output.find("R2E_START_TEST_OUTPUT")
    end = output.find("R2E_END_TEST_OUTPUT")
    if start == -1:
        return output
    chunk = output[start:end] if end > start else output[start:]
    # Drop the marker line itself
    nl = chunk.find("\n")
    return chunk[nl + 1 :] if nl != -1 else chunk


def validate_pr(
    *,
    sandbox: DockerSandbox,
    base_commit: str,
    patch: str,
    test_patch: str,
    test_cmds: list[str],
    language: str | None = None,
    timeout: int = 600,
) -> ValidationOutcome:
    """Run the two-stage validation and return the resulting outcome.

    Re-uses a shared sandbox across PRs; the `git reset --hard` at the top of
    each stage script guarantees a clean working tree.

    `language` is the LanguageHint value (e.g. "python", "go") used as a
    fallback when test_cmds doesn't name a known runner. Almost always the
    runner is inferred from test_cmds — language is just a safety net.
    """
    if not test_cmds:
        return ValidationOutcome(
            status="failed",
            reason="bootstrap did not record any test_cmds",
        )

    # Ensure the base commit is in the container's object db before any
    # `git reset --hard <sha>`. Bootstrap shallow-clones at depth=1, so
    # historical PR base commits aren't present by default.
    if not _fetch_base_commit(sandbox, base_commit, timeout=timeout):
        return ValidationOutcome(
            status="failed",
            reason=f"could not fetch base_commit {base_commit[:12]} in sandbox",
        )
    if not _ensure_python(sandbox):
        return ValidationOutcome(
            status="failed",
            reason="no system python3 for the grader launcher in the bootstrap image",
        )
    clock = _Stopwatch()

    def read_base(path: str) -> str | None:
        shown = sandbox.exec(
            f"git -C /workspace show {base_commit}:{shlex.quote(path)} 2>/dev/null", timeout=30
        )
        return shown.stdout if shown.ok else None

    # npm/yarn scripts and jest/mocha/vitest settings resolved against base, once.
    test_cmds = freeze_node_commands(test_cmds, read_base)

    # Stage 1: pre-fix (apply test_patch only) — captures the "buggy" baseline.
    pre_script = _build_stage_script(
        base_commit,
        apply_patch=None,
        apply_test_patch=test_patch,
        test_cmds=test_cmds,
        language=language,
        staged=_stage(sandbox, test_patch=test_patch),
        keep_collecting=True,
    )
    logger.info("validate_pr: running pre-fix stage at %s", base_commit[:12])
    pre = clock.run(sandbox, pre_script, timeout)
    # Parse the FULL output — a real suite emits 100k+ chars of per-test lines;
    # truncating to 20k elided the test section and zeroed F2P detection.
    pre_log = pre.truncated(max_chars=5_000_000)
    pre_status = parse_logs(test_cmds, _slice_test_output(pre_log), language=language)

    if _UNSAFE_IMAGE in pre_log:
        return ValidationOutcome(
            test_cmds=test_cmds,
            status="failed",
            reason="unsafe image: a path root runs code from is writable by the test user",
            pre_log=pre_log,
        )
    rebuilt = _REBUILD_RE.search(pre_log)
    if not rebuilt or rebuilt.group(1) != "ok":
        return ValidationOutcome(
            test_cmds=test_cmds,
            status="failed",
            reason=f"base tree does not survive the submission rebuild ({_status(rebuilt)})",
            pre_log=pre_log,
        )

    # If the test_patch itself failed to apply, no point continuing.
    if "error: patch failed" in pre_log.lower() or "patch does not apply" in pre_log.lower():
        return ValidationOutcome(
            test_cmds=test_cmds,
            status="failed",
            reason="test_patch failed to apply at base_commit",
            pre_log=pre_log,
        )

    # Stage 2: post-fix (apply both patch and test_patch).
    post_script = _build_stage_script(
        base_commit,
        apply_patch=patch,
        apply_test_patch=test_patch,
        test_cmds=test_cmds,
        language=language,
        staged=_stage(sandbox, patch=patch, test_patch=test_patch),
    )
    logger.info("validate_pr: running post-fix stage")
    post = clock.run(sandbox, post_script, timeout)
    post_log = post.truncated(max_chars=5_000_000)
    post_status = parse_logs(test_cmds, _slice_test_output(post_log), language=language)

    rebuilt = _REBUILD_RE.search(post_log)
    if rebuilt and rebuilt.group(1) != "ok":
        # The grader would drop part of the fix, or refuse it: the oracle
        # could never score 1 (e.g. it edits a path the grader treats as a test).
        return ValidationOutcome(
            test_cmds=test_cmds,
            status="failed",
            reason=f"gold patch does not survive the submission rebuild ({_status(rebuilt)})",
            pre_log=pre_log,
            post_log=post_log,
        )
    if "error: patch failed" in post_log.lower() or "patch does not apply" in post_log.lower():
        return ValidationOutcome(
            test_cmds=test_cmds,
            status="failed",
            reason="gold patch failed to apply at base_commit",
            pre_log=pre_log,
            post_log=post_log,
        )

    # Repeat the post-fix stage; a test is tracked only if every run agrees.
    reruns = []
    for attempt in range(2, POST_FIX_RUNS + 1):
        logger.info("validate_pr: post-fix rerun %d/%d", attempt, POST_FIX_RUNS)
        rerun = clock.run(sandbox, post_script, timeout).truncated(max_chars=5_000_000)
        reruns.append(parse_logs(test_cmds, _slice_test_output(rerun), language=language))
    flaky = flaky_tests([post_status, *reruns])
    if flaky:
        logger.info("validate_pr: dropping %d flaky test(s)", len(flaky))

    # Compute F2P / P2P. Both sets are over the union of test names seen.
    #
    # F2P counts ERROR->PASSED as well as FAILED->PASSED: a new test that
    # references a symbol the fix introduces *errors* (import/collection
    # failure) at base_commit rather than asserting-failing. Restricting F2P
    # to FAILED-only silently drops these, killing otherwise-valid candidates
    # on `no_fail_to_pass`. Both transitions mean "broken at base, fixed by
    # the gold patch" — the FAIL_TO_PASS contract.
    fail_to_pass: list[str] = []
    pass_to_pass: list[str] = []
    for tname, pre_st in pre_status.items():
        if tname in flaky:
            continue
        post_st = post_status.get(tname)
        if pre_st in ("FAILED", "ERROR") and post_st == "PASSED":
            fail_to_pass.append(tname)
        elif pre_st == "PASSED" and post_st == "PASSED":
            pass_to_pass.append(tname)
    # A test module that can't be imported at base, typically because it
    # imports what the fix adds, has no test IDs there: pytest reports one
    # file-level ERROR. Its tests failed at base all the same.
    uncollected = {name for name, st in pre_status.items() if st == "ERROR" and "::" not in name}
    promoted: dict[str, int] = {}
    for tname, post_st in post_status.items():
        if tname in pre_status or tname in flaky or post_st != "PASSED":
            continue
        module = tname.split("::", 1)[0]
        if module in uncollected:
            fail_to_pass.append(tname)
            promoted[module] = promoted.get(module, 0) + 1
    if promoted:
        logger.info(
            "validate_pr: fail-to-pass tests inferred from base collection errors: %s", promoted
        )

    # Diagnostics: when yield is poor, this tells us whether the suite even
    # ran (test counts) and where transitions went — distinguishing "genuine
    # non-bugfix PR" from "validation didn't detect the failing test".
    logger.info(
        "validate_pr: parsed pre=%d post=%d tests; f2p=%d p2p=%d (pre statuses: %s)",
        len(pre_status),
        len(post_status),
        len(fail_to_pass),
        len(pass_to_pass),
        {s: sum(1 for v in pre_status.values() if v == s) for s in set(pre_status.values())},
    )

    if not fail_to_pass:
        return ValidationOutcome(
            test_cmds=test_cmds,
            status="failed",
            reason="no fail-to-pass tests after validation",
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            pre_log=pre_log,
            post_log=post_log,
            flaky=sorted(flaky),
        )

    mutants = _validate_mutants(
        sandbox=sandbox,
        base_commit=base_commit,
        patch=patch,
        test_patch=test_patch,
        test_cmds=test_cmds,
        language=language,
        timeout=timeout,
        targets=sorted(fail_to_pass),
        tracked=sorted(fail_to_pass) + sorted(pass_to_pass),
        post_status=post_status,
        clock=clock,
    )
    timing = measure(clock.samples, clock.copies)
    timing, checked = fit_runs(timing, mutants is not None)
    mutants = mutants if checked else None
    if timing is not None and timing.over_cap:
        return ValidationOutcome(
            test_cmds=test_cmds,
            status="failed",
            reason=(
                f"grading would need a {timing.needed_verifier_timeout} s verifier timeout, "
                "over the cap"
            ),
            pre_log=pre_log,
            post_log=post_log,
            flaky=sorted(flaky),
            timing=timing,
        )
    return ValidationOutcome(
        test_cmds=test_cmds,
        status="verified",
        fail_to_pass=sorted(fail_to_pass),
        pass_to_pass=sorted(pass_to_pass),
        pre_log=pre_log,
        post_log=post_log,
        flaky=sorted(flaky),
        timing=timing,
        mutants=mutants,
    )


def flaky_tests(runs: list[dict[str, str]]) -> set[str]:
    """Tests whose status is not the same in every run (absent counts as a status)."""
    names = set().union(*runs) if runs else set()
    return {name for name in names if len({run.get(name) for run in runs}) > 1}


def _read_workspace_file(sandbox: DockerSandbox, path: str) -> str | None:
    result = sandbox.exec(f"base64 < /workspace/{shlex.quote(path)} | tr -d '\\n'", timeout=30)
    if not result.ok:
        return None
    try:
        return base64.b64decode(result.stdout.strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _validate_mutants(
    *,
    sandbox: DockerSandbox,
    base_commit: str,
    patch: str,
    test_patch: str,
    test_cmds: list[str],
    language: str | None,
    timeout: int,
    targets: list[str],
    tracked: list[str],
    post_status: dict[str, str],
    clock: _Stopwatch,
) -> Mutants | None:
    """Mutants (of fail-to-pass `targets`) that fail on the reference fix, and
    decoys (of other `tracked` tests) that don't move, through the grading path.

    Runs right after the post-fix stages, while /workspace still holds the
    fixed tree with the hidden tests, so the test files are read from there.
    `tracked` holds only tests that passed in every post-fix run. No usable
    mutant means `tamper_check` is "unavailable" at grading; the task is never
    rejected for this.
    """
    hidden = set(_files_in_patch(test_patch))
    seed = hashlib.sha256(f"{base_commit}\0{test_patch}".encode()).hexdigest()

    def read(path: str) -> str | None:
        return _read_workspace_file(sandbox, path)

    # Python through its own AST; Go, Rust and JS/TS through tree-sitter.
    mutants = make_mutants(
        targets,
        read,
        allowed_files={p for p in hidden if p.endswith(".py")},
        seed=seed,
        decoys=tracked,
    ) or make_tree_sitter_mutants(
        targets,
        read,
        allowed_files={p for p in hidden if language_of(p)},
        seed=seed,
        decoys=tracked,
    )
    if mutants is None:
        return None
    script = _build_stage_script(
        base_commit,
        apply_patch=patch,
        apply_test_patch=test_patch,
        test_cmds=test_cmds,
        language=language,
        staged=_stage(sandbox, patch=patch, test_patch=test_patch, mutants=mutants),
    )
    logger.info("validate_pr: running the mutant stage (%d tests)", len(mutants.ids))
    output = clock.run(sandbox, script, timeout).truncated(max_chars=5_000_000)
    exit_code = _EXIT_RE.search(output)
    fixed = parse_logs(test_cmds, _slice_test_output(output), language=language)
    kept = validated(mutants, fixed=post_status, fixed_with_mutants=fixed, tracked=tracked)
    if kept is None or exit_code is None:
        logger.info("validate_pr: no usable mutants for this task")
        return None
    kept.exit_nonzero = int(exit_code.group(1)) != 0
    return kept
