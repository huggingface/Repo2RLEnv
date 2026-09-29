"""Validation must see runners that report on stderr, not just stdout."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from repo2rlenv.log_parsers import parse_logs
from repo2rlenv.pipelines.pr_runtime_validate import _build_stage_script, _slice_test_output

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_BASE = "a" * 40

# Real `npx jest --verbose` output (jest 30.5.2). Every line of it, including
# the per-test results, is written to stderr.
_JEST_STDERR = (
    "FAIL jt/math.test.js\n"
    "  Math\n"
    "    ✓ adds\n"
    "    ✕ breaks (1 ms)\n"
    "\n"
    "  ● Math › breaks\n"
    "\n"
    "    expect(received).toBe(expected) // Object.is equality\n"
    "\n"
    "    Expected: 3\n"
)


def test_test_block_redirects_stderr_into_the_markers():
    script = _build_stage_script(
        _BASE, apply_patch=None, apply_test_patch=None, test_cmds=["npx jest --verbose"]
    )
    lines = script.splitlines()
    # The braces sit on their own lines so a command ending in a comment, a
    # semicolon or a heredoc stays valid.
    assert lines[lines.index("{") + 1] == "npx jest --verbose"
    assert lines[lines.index("{") + 2] == "} 2>&1"
    start = lines.index("echo R2E_START_TEST_OUTPUT")
    end = lines.index("echo R2E_END_TEST_OUTPUT")
    # Without `set +x` the shell's own trace would land inside the slice.
    assert lines[start + 1] == "set +x"
    assert lines[end - 1] == "set -x"


def test_multiple_test_cmds_share_one_redirect():
    script = _build_stage_script(
        _BASE,
        apply_patch=None,
        apply_test_patch=None,
        test_cmds=["export PATH=/opt/bin:$PATH", "npx jest --verbose"],
    )
    lines = script.splitlines()
    assert lines[lines.index("{") + 1] == "export PATH=/opt/bin:$PATH && npx jest --verbose"
    assert lines.count("} 2>&1") == 1


def test_empty_test_cmds_still_produce_a_block():
    script = _build_stage_script(_BASE, apply_patch=None, apply_test_patch=None, test_cmds=[])
    lines = script.splitlines()
    assert lines[lines.index("{") + 1] == "echo 'no test_cmds'"


def test_redirected_output_survives_the_slice_and_parses():
    # What `truncated()` builds once the block redirects: the runner's output
    # sits between the markers on stdout, and only the shell trace is left in
    # the stderr section that follows.
    combined = (
        "+ git reset --hard\n"
        "R2E_START_TEST_OUTPUT\n"
        + _JEST_STDERR
        + "R2E_END_TEST_OUTPUT\n"
        + "\n--- stderr ---\n"
        + "+ echo R2E_START_TEST_OUTPUT\n+ set -x\n"
    )
    sliced = _slice_test_output(combined)
    assert "git reset" not in sliced
    assert parse_logs(["npx jest --verbose"], sliced) == {
        "jt/math.test.js > Math > adds": "PASSED",
        "jt/math.test.js > Math > breaks": "FAILED",
    }


def test_unredirected_stderr_would_be_sliced_away():
    # The shape before this change: the runner's output landed after the end
    # marker, so the slice was empty and F2P detection saw nothing.
    combined = "R2E_START_TEST_OUTPUT\nR2E_END_TEST_OUTPUT\n\n--- stderr ---\n" + _JEST_STDERR
    assert _slice_test_output(combined).strip() == ""


# --- the generated script has to be valid bash and actually capture stderr ---

_SHELL_CASES = {
    "plain": ["pytest -v"],
    "trailing comment": ["pytest -v # smoke"],
    "trailing semicolon": ["pytest -v;"],
    "heredoc": ["python3 - <<'PY'\nprint('from heredoc')\nPY"],
    "two commands": ["export R2E_MARK=1", "pytest -v"],
    "pipeline": ["pytest -v | cat"],
    "quoted semicolon": ["""python3 -c "print('a; b')\""""],
    "empty": [],
}


def _run_test_block(test_cmds: list[str]) -> tuple[str, int]:
    """Execute only the marker block of the generated script, as bash sees it."""
    script = _build_stage_script(
        _BASE, apply_patch=None, apply_test_patch=None, test_cmds=test_cmds
    )
    lines = script.splitlines()
    start = lines.index("echo R2E_START_TEST_OUTPUT")
    end = lines.index("echo R2E_END_TEST_OUTPUT")
    block = "\n".join(["set -uxo pipefail", *lines[start : end + 1]])
    proc = subprocess.run(["bash", "-c", block], capture_output=True, text=True)
    combined = proc.stdout
    if proc.stderr.strip():
        combined += "\n--- stderr ---\n" + proc.stderr
    return _slice_test_output(combined), proc.returncode


@needs_bash
@pytest.mark.parametrize("label", sorted(_SHELL_CASES))
def test_generated_script_is_valid_bash(label):
    script = _build_stage_script(
        _BASE, apply_patch=None, apply_test_patch=None, test_cmds=_SHELL_CASES[label]
    )
    proc = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert proc.returncode == 0, f"{label}: {proc.stderr}"


@needs_bash
def test_heredoc_output_lands_inside_the_markers():
    sliced, _ = _run_test_block(["python3 - <<'PY'\nprint('from heredoc')\nPY"])
    assert sliced.strip() == "from heredoc"


@needs_bash
def test_stderr_of_the_test_command_is_captured():
    sliced, _ = _run_test_block(["python3 -c \"import sys; print('to stderr', file=sys.stderr)\""])
    assert "to stderr" in sliced


@needs_bash
def test_shell_trace_stays_out_of_the_sliced_block():
    sliced, _ = _run_test_block(["echo hello"])
    assert sliced.strip() == "hello"
    assert "+ echo" not in sliced


@needs_bash
def test_nonzero_test_exit_still_closes_the_block():
    # `set -uxo pipefail` has no -e, so a failing suite must not abort the
    # script before the end marker is printed.
    sliced, returncode = _run_test_block(["python3 -c \"import sys; print('ran'); sys.exit(1)\""])
    assert sliced.strip() == "ran"
    assert returncode == 0


@needs_bash
def test_trailing_comment_and_semicolon_still_run():
    assert _run_test_block(["echo ok # trailing comment"])[0].strip() == "ok"
    assert _run_test_block(["echo ok;"])[0].strip() == "ok"
