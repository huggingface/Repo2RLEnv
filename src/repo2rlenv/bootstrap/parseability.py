"""Check that a repo's saved test commands print per-test results we can parse.

Runtime pipelines classify tests as fail-to-pass / pass-to-pass from the runner's
output (`log_parsers`). A command that exits 0 but prints only a summary line, or
uses a custom reporter, can never yield a task, however good the PR is. The
bootstrap agent checks this before it saves its setup, while it can still pick
another command.
"""

from __future__ import annotations

import logging

from repo2rlenv.bootstrap.docker import DockerSandbox
from repo2rlenv.log_parsers import parse_logs

logger = logging.getLogger(__name__)

# How much runner output the agent sees when its command is rejected.
_TAIL_CHARS = 1500

SUPPORTED_RUNNERS = (
    "pytest -v, python -m unittest -v, go test, cargo test / cargo nextest, "
    "jest --verbose, mocha --reporter spec, vitest --reporter=verbose, "
    "or TAP (qunit, tape, node-tap, any runner with --reporter tap)"
)


def runtime_test_cmds(test_cmds: list[str]) -> list[str]:
    """The commands as `pr_runtime` runs them (verbosity flags added, pipes stripped)."""
    # Imported here: `pipelines.pr_runtime` imports `bootstrap.spec` at module load.
    from repo2rlenv.pipelines.pr_runtime import normalize_test_cmds_for_runtime

    return normalize_test_cmds_for_runtime(test_cmds)


def count_parsed_tests(test_cmds: list[str], log: str, *, language: str | None) -> int:
    """Number of per-test results `parse_logs` reads from `log`."""
    return len(parse_logs(test_cmds, log, language=language))


def probe_parseable(
    sandbox: DockerSandbox,
    test_cmds: list[str],
    *,
    language: str | None,
    timeout: int = 600,
) -> tuple[int, int, str]:
    """Run `test_cmds` as the runtime pipelines will; return (parsed, exit_code, output tail)."""
    cmds = runtime_test_cmds(test_cmds)
    result = sandbox.exec(" && ".join(cmds), timeout=timeout)
    log = result.stdout + "\n" + result.stderr
    parsed = count_parsed_tests(cmds, log, language=language)
    return parsed, result.exit_code, log.strip()[-_TAIL_CHARS:]


def rejection_message(exit_code: int, tail: str) -> str:
    return (
        "SAVE_SETUP rejected: your test_cmds ran (exit="
        f"{exit_code}) but printed no per-test results that can be parsed. Downstream "
        "pipelines decide which tests fail before a fix and pass after it from one "
        "result line per test, so a command that prints only a summary, or that uses a "
        "custom reporter, cannot be used. Save a command whose output has one line per "
        f"test with its name and status, in the format of: {SUPPORTED_RUNNERS}. Invoke "
        "the framework's own CLI with such a reporter instead of a wrapper script that "
        "hides it. Do not use --collect-only: it prints names without results.\n"
        f"Last output:\n{tail or '(no output)'}"
    )
