"""Validation must see runners that report on stderr, not just stdout."""

from __future__ import annotations

import shlex
import shutil
import subprocess

import pytest

from repo2rlenv.log_parsers import parse_logs
from repo2rlenv.pipelines.pr_runtime_validate import (
    _build_stage_script,
    _slice_test_output,
    _test_block,
)

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


def test_test_block_runs_through_the_launcher():
    script = _build_stage_script(
        _BASE, apply_patch=None, apply_test_patch=None, test_cmds=["npx jest --verbose"]
    )
    lines = script.splitlines()
    launch = next(line for line in lines if line.startswith('R2E_TEST_EXIT="$('))
    # As the grader runs it: image python with -I -S, unprivileged, one bash -c.
    assert '"$R2E_PY" -B -I -S /opt/r2e-validate/launcher.py' in launch
    assert launch.endswith(f'-- {shlex.quote("npx jest --verbose")})"')
    start = lines.index("echo R2E_START_TEST_OUTPUT")
    end = lines.index("echo R2E_END_TEST_OUTPUT")
    # The launcher's log (stdout and stderr) sits between the markers, and the
    # shell trace is off around it.
    assert lines[start + 1] == "cat /opt/r2e-validate/test_output.log"
    assert end == start + 2
    assert "set +x" in lines[: lines.index(launch)]
    assert lines[end + 1] == 'echo "R2E_TEST_EXIT_CODE=${R2E_TEST_EXIT}"'


def test_multiple_test_cmds_share_one_shell():
    script = _build_stage_script(
        _BASE,
        apply_patch=None,
        apply_test_patch=None,
        test_cmds=["export PATH=/opt/bin:$PATH", "npx jest --verbose"],
    )
    block = "export PATH=/opt/bin:$PATH && npx jest --verbose"
    assert _test_block(["export PATH=/opt/bin:$PATH", "npx jest --verbose"]) == block
    assert f"-- {shlex.quote(block)})" in script


def test_rust_tasks_write_a_fresh_copy_of_cargo_home():
    script = _build_stage_script(
        _BASE, apply_patch=None, apply_test_patch=None, test_cmds=["cargo test"], language="rust"
    )
    # A per-run copy: the image's CARGO_HOME stays root-owned, so nothing one
    # run writes there reaches the other.
    assert '--writable /workspace --fresh-copy CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"' in script
    assert '--writable "${CARGO_HOME' not in script
    # The rustup proxies in that copy read the toolchains, which stay read-only.
    assert '--readable "${RUSTUP_HOME:-$HOME/.rustup}"' in script


def test_go_tasks_may_read_the_module_cache():
    # Go keeps its module cache under root's 0700 home; the test user reads it.
    script = _build_stage_script(
        _BASE, apply_patch=None, apply_test_patch=None, test_cmds=["go test ./..."], language="go"
    )
    assert '--readable "${GOMODCACHE:-${GOPATH:-$HOME/go}/pkg/mod}"' in script
    assert "--writable /workspace --readable" in script


@pytest.mark.parametrize("cmd", ["pytest -v", "go test ./...", "npx jest"])
def test_config_reset_runs_after_the_fix_and_before_the_test_patch(cmd):
    """Validation grades like test.sh: runner config is back at base before the
    hidden test patch, for every runner, so a gold patch's own conftest.py,
    go.work or .npmrc doesn't count."""
    script = _build_stage_script(
        _BASE, apply_patch="FIX", apply_test_patch="TESTS", test_cmds=[cmd]
    )
    reset = script.find("R2E_PYTEST_CONFIG_GUARD")
    assert script.find("FIX") < reset < script.find("TESTS") < script.find(shlex.quote(cmd))
    assert "|| exit 1" in script[reset : script.find("TESTS")]
    assert '"$R2E_PY" -B -I -S -' in script[:reset]


def test_empty_test_cmds_still_produce_a_block():
    script = _build_stage_script(_BASE, apply_patch=None, apply_test_patch=None, test_cmds=[])
    block = shlex.quote("echo 'no test_cmds'")
    assert f"-- {block})" in script


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
    """Run the test block the way the launcher does: `bash -c`, stderr into the log."""
    proc = subprocess.run(
        ["bash", "-c", _test_block(test_cmds)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    marked = f"R2E_START_TEST_OUTPUT\n{proc.stdout}R2E_END_TEST_OUTPUT\n"
    return _slice_test_output(marked), proc.returncode


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
def test_nonzero_test_exit_is_reported():
    sliced, returncode = _run_test_block(["python3 -c \"import sys; print('ran'); sys.exit(1)\""])
    assert sliced.strip() == "ran"
    assert returncode == 1


@needs_bash
def test_trailing_comment_and_semicolon_still_run():
    assert _run_test_block(["echo ok # trailing comment"])[0].strip() == "ok"
    assert _run_test_block(["echo ok;"])[0].strip() == "ok"


@needs_bash
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_staged_patch_over_the_argv_cap_applies(tmp_path):
    import base64

    from repo2rlenv.pipelines.pr_runtime_validate import _PATCH, _apply

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    (repo / "m.py").write_text("".join(f"line_{i} = {i}\n" for i in range(5000)))
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=repo, check=True)
    (repo / "m.py").write_text("".join(f"line_{i} = {i} + 1\n" for i in range(5000)))
    patch = subprocess.run(
        ["git", "diff"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert len(patch.encode()) > 128 * 1024
    subprocess.run(["git", "checkout", "--", "m.py"], cwd=repo, check=True)

    staged = tmp_path / "0.b64"
    staged.write_bytes(base64.b64encode(patch.encode()))
    line = _apply(patch, {_PATCH: str(staged)}, _PATCH)
    assert len(line.encode()) < 1024  # the patch itself stays out of the script
    proc = subprocess.run(["bash", "-c", line], cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert (repo / "m.py").read_text().startswith("line_0 = 0 + 1\n")


def test_stage_exports_the_guard_settings():
    script = _build_stage_script(
        _BASE, apply_patch=None, apply_test_patch=None, test_cmds=["cargo test"], language="rust"
    )
    assert 'R2E_GUARD_ENV="$(mktemp)"' in script and '. "$R2E_GUARD_ENV"' in script
