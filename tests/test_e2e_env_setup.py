"""End-to-end tests for the `env_setup` pipeline (RFC 0008 §10).

Two categories, gated differently:

  1. **Generation E2E** (`test_e2e_pallets_click_generates_and_oracle_resolves`)
     — the real pipeline against `pallets/click` at HEAD: a full bootstrap
     agent loop (many LLM calls), LLM recipe distillation (more LLM calls),
     multiple Docker image builds, and a `harbor run -a oracle`. This is NOT
     `tests/test_e2e_public.py`'s cost class — that file's `gh`-only gate
     covers `pr_diff`, which is text-only generation with no Docker and no
     image builds. This test's cost class is `tests/test_e2e_hub_build.py`'s
     ("needs docker+harbor+network" and real LLM spend), so it carries that
     file's manual opt-in env-var gate (`R2E_E2E_ENV_SETUP=1`) IN ADDITION TO
     the capability skips (docker/gh/harbor/LLM key) — never run by a plain
     `pytest` invocation, opt-in only.

  2. **Container-only gate assertions** (the three `test_gate_half_*` /
     `test_gate_*` functions below) — exercise gate 0 / gate 1/2 / gate 1
     machinery for real inside a container, against a small SYNTHETIC repo
     built entirely from `RUN` instructions (no external clone, no LLM call
     anywhere in these three tests — they never touch `EnvSetupPipeline`,
     bootstrap, or recipe distillation). Docker time is free and CI-
     appropriate in a way LLM spend is not, so these three stay on the plain
     Docker-daemon gate only — no opt-in var. `tests/test_env_setup_artifacts.py`
     already covers this same machinery locally via path-rewriting
     (`_stage_task` / `_run_test_sh`); what these add is a REAL container:
     a real git worktree, a real venv, real `git clean` — the one thing a
     path-rewritten local subprocess cannot exercise.

Docker daemon (not just the binary) is required for every test in this file
— see `bootstrap/docker.py:is_docker_available` and Task 7's fix for the
`shutil.which("docker")`-is-true-but-stopped trap.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

import pytest

from repo2rlenv.auth import resolve_llm_api_key
from repo2rlenv.bootstrap.docker import is_docker_available
from repo2rlenv.pipelines._env_setup_artifacts import build_env_setup_aux_files
from repo2rlenv.pipelines._setup_recipe import EnvSetupSandbox
from repo2rlenv.pipelines.env_setup import _dry_run_gates

pytestmark = pytest.mark.skipif(
    not is_docker_available(), reason="docker daemon required for env_setup E2E"
)

# Opt-in gate for the PAID generation E2E only (see module docstring for the
# cost-class distinction from the three container-only tests below). Same
# shape as tests/test_e2e_hub_build.py's R2E_E2E_HUB_BUILD.
_ENV_SETUP_E2E_VAR = "R2E_E2E_ENV_SETUP"

_BASE_IMAGE = "python:3.12-slim"

# Forbidden install commands in an EMITTED env_setup Dockerfile — the single
# mistake that silently voids the pipeline (installing the repo's own deps
# bakes the answer in). Kept as its own small list rather than importing the
# one in tests/test_env_setup_artifacts.py, which is private to that module.
_FORBIDDEN_DOCKERFILE_INSTALLS = (
    "pip install",
    "pip3 install",
    "uv pip",
    "npm install",
    "npm ci",
    "yarn install",
    "pnpm install",
    "cargo build",
    "cargo fetch",
    "go mod download",
    "poetry install",
    "mvn install",
)


def _gh_authenticated() -> bool:
    """Copied from `tests/test_e2e_public.py` — same gate, same failure posture."""
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=5, check=False)
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


_HAS_GH = shutil.which("gh") is not None
_HAS_LLM_KEY = resolve_llm_api_key("anthropic") is not None


def _unique_tag(name: str) -> str:
    return f"r2e-e2e-envsetup-{name}-{uuid.uuid4().hex[:8]}:test"


def _shell_single_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _synthetic_python_dockerfile(test_body: str) -> str:
    """A from-scratch bare Python repo with no external clone: a `git init`,
    one tracked test file, one commit — done entirely via `RUN` instructions
    so the three gate assertions below depend on nothing but the base image
    (no GitHub reachability, no dependence on some real repo's test suite
    shape). `pytest` is baked into the image itself (not installed by an
    emitted recipe) — these tests exercise gate 0/½/1 machinery, not the
    agent-installs-from-bare story, which is `pallets/click`'s job below.

    `test_body` is embedded as a `printf '%b'` with real newlines converted
    to literal `\\n` escapes, because a literal newline inside a Dockerfile
    `RUN` line would end the instruction early.
    """
    escaped = test_body.replace("\\", "\\\\").replace("\n", "\\n")
    quoted = _shell_single_quote(escaped)
    return (
        f"FROM {_BASE_IMAGE}\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends git "
        "&& rm -rf /var/lib/apt/lists/*\n"
        "RUN pip install --no-cache-dir --quiet pytest\n"
        "WORKDIR /workspace\n"
        "RUN git init -q && git config user.email t@t.example && git config user.name t\n"
        "RUN mkdir -p tests\n"
        f"RUN printf '%b' {quoted} > tests/test_thing.py\n"
        "RUN git add -A && git commit -q -m base\n"
    )


# ---------------------------------------------------------------------------
# Step 2 — generation E2E (opt-in: needs docker + gh + harbor + an LLM key,
# and spends real LLM budget — see R2E_E2E_ENV_SETUP above)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get(_ENV_SETUP_E2E_VAR) != "1",
    reason=(
        f"set {_ENV_SETUP_E2E_VAR}=1 to run the env_setup generate+bootstrap+oracle "
        "smoke (needs docker+harbor+network+LLM key; spends real LLM budget)"
    ),
)
@pytest.mark.skipif(not _HAS_GH, reason="gh CLI not available — skipping end-to-end tests")
@pytest.mark.skipif(not _gh_authenticated(), reason="gh not authenticated")
@pytest.mark.skipif(
    not _HAS_LLM_KEY, reason="no LLM API key configured (ANTHROPIC_API_KEY) — recipe distillation"
)
def test_e2e_pallets_click_generates_and_oracle_resolves(tmp_path: Path) -> None:
    """`pallets/click` at HEAD, `limit=1`: the real pipeline end to end, plus
    an independent `harbor run -a oracle` (mirrors
    `tests/test_e2e_hub_build.py`'s subprocess shape) rather than trusting
    the pipeline's own internal oracle gate silently.

    Slow (a real bootstrap + LLM recipe distillation + two container builds)
    and not free (real LLM spend) — that cost is why this carries a manual
    opt-in env-var gate (`R2E_E2E_ENV_SETUP=1`) on top of the docker/gh/LLM-
    key/harbor capability skips, matching `tests/test_e2e_hub_build.py`'s
    shape rather than `tests/test_e2e_public.py`'s auto-run one (that file's
    `pr_diff` run is text-only — no Docker, no image builds).
    """
    if shutil.which("harbor") is None:
        pytest.skip("harbor CLI required for the oracle-run assertion")

    from repo2rlenv.pipelines.env_setup import EnvSetupPipeline
    from repo2rlenv.spec.input import (
        GenerationInput,
        LLMSpec,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )
    from repo2rlenv.spec.options import EnvSetupOptions

    gen_input = GenerationInput(
        repo=RepoSpec(url="pallets/click", ref="HEAD", access="public"),
        pipeline=PipelineSpec(name=PipelineName.ENV_SETUP, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(
            destination=str(tmp_path), org="r2e-e2e", dataset_name="env-setup-e2e-click"
        ),
    )
    options = EnvSetupOptions(limit=1)
    pipeline = EnvSetupPipeline(gen_input, options)

    result = pipeline.run(tmp_path)
    assert result.emitted >= 1, f"expected >=1 emitted task, skips: {result.skip_reasons}"

    task_dirs = sorted(d for d in tmp_path.iterdir() if d.is_dir() and d.name != "jobs")
    assert task_dirs, "no task directory emitted"
    task_dir = task_dirs[0]

    assert (task_dir / "task.toml").is_file()
    assert (task_dir / "instruction.md").is_file()

    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    for forbidden in _FORBIDDEN_DOCKERFILE_INSTALLS:
        assert forbidden not in dockerfile, f"emitted Dockerfile bakes in a dependency: {forbidden}"

    f2p = json.loads((task_dir / "tests" / "f2p.json").read_text())
    assert f2p, "f2p.json must be non-empty — a gradeable target-test set"

    # Independent oracle check: copy the emitted task into a scratch dataset
    # (harbor's `-p` runs every task under it) and grade the gold recipe.
    jobs = tmp_path / "harbor-jobs"
    dataset = tmp_path / "harbor-dataset"
    dataset.mkdir()
    shutil.copytree(task_dir, dataset / task_dir.name)
    proc = subprocess.run(
        [
            "harbor",
            "run",
            "-p",
            str(dataset),
            "-a",
            "oracle",
            "--env",
            "docker",
            "-n",
            "1",
            "-y",
            "--quiet",
            "--jobs-dir",
            str(jobs),
        ],
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    assert proc.returncode == 0, f"harbor run failed: {proc.stderr[-2000:]}"

    rewards = list(jobs.glob("*/*/verifier/reward.txt"))
    assert rewards, "no reward.txt produced by harbor run -a oracle"
    val = float(rewards[0].read_text().strip())
    assert val == 1.0, f"oracle reward {val} != 1.0 for {task_dir.name}"


# ---------------------------------------------------------------------------
# Step 3.1 — gate 1/2 cleans a report-forging hook, measured where it helps
# ---------------------------------------------------------------------------

_MAKEREPORT_HOOK = (
    "import pytest\n"
    "\n"
    "@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    outcome = yield\n"
    "    report = outcome.get_result()\n"
    "    if report.when == 'call':\n"
    "        report.outcome = 'passed'\n"
    "        report.longrepr = None\n"
)


def test_gate_half_cleans_a_report_forging_hook_where_it_would_otherwise_pass() -> None:
    """Asserting "reward != 1.0" in an already-green container proves nothing
    (gate ½ removes the hook, the suite genuinely passes, and the reward is
    1.0 either way — this is the exact vacuity trap RFC 0008 §10 calls out).

    So this test is built to make the counterfactual REAL, not assumed:

      1. The suite is genuinely red (`assert False`) with no hook — that is
         the "setup.sh was not run" state.
      2. Plant `/workspace/conftest.py` with a `pytest_runtest_makereport`
         hookwrapper that force-rewrites every `call`-phase report's
         `outcome` to `"passed"` (the standard pytest idiom for this — it
         mutates the report object handed onward to `pytest_runtest_logreport`,
         so both the printed `PASSED` line and the process exit code follow).
      3. Run the SAME capture + the SAME shipped `verifier.py` against that
         hook, with NO restore step — this is what "without gate ½" means,
         computed for real rather than described in prose — and confirm it
         actually scores 1.0. This is the step that makes the test
         non-vacuous: if the hook could not flip the answer, this assertion
         would fail here, before gate ½ is even in the picture.
      4. THEN run the real, shipped `tests/test.sh` (gate 0 no-op -> gate ½
         restore+clean, which removes the untracked `conftest.py` because it
         matches the `conftest.py` pathspec -> gate 1 reruns the suite fresh)
         and confirm the reward drops to 0.0, and the hook file is gone.
    """
    test_cmd = "python -m pytest -v tests/test_thing.py"
    f2p = ["tests/test_thing.py::test_should_fail"]
    dockerfile = _synthetic_python_dockerfile(
        "def test_should_fail():\n    assert False, 'genuinely red without the hook'\n"
    )
    sandbox = EnvSetupSandbox.build_and_start(dockerfile, tag=_unique_tag("gate1"))
    try:
        aux = build_env_setup_aux_files(
            language="python",
            test_cmds=[test_cmd],
            runner="pytest",
            probe="none",
            base_commit="HEAD",
            package=None,
            dist_name=None,
            f2p=f2p,
            p2p=[],
        )
        sandbox.put_files(aux, "/tests")
        chmod = sandbox.exec("chmod +x /tests/*.sh", timeout=60)
        assert chmod.exit_code == 0, chmod.stderr

        plant = sandbox.exec(
            f"printf '%s' {_shell_single_quote(_MAKEREPORT_HOOK)} > /workspace/conftest.py",
            timeout=30,
        )
        assert plant.exit_code == 0, plant.stderr

        # (3) The counterfactual: capture + verify, no restore in between.
        counterfactual = sandbox.exec(
            "set -o pipefail\n"
            "cd /workspace\n"
            f"( {test_cmd} ) > /tmp/counterfactual.log 2>&1\n"
            "EXIT=$?\n"
            "mkdir -p /tmp/counterfactual_out\n"
            "python3 /tests/verifier.py "
            "--log /tmp/counterfactual.log "
            "--f2p /tests/f2p.json --p2p /tests/p2p.json "
            "--runner pytest "
            f"--test-cmds '{test_cmd}' "
            '--exit-code "$EXIT" '
            "--out-dir /tmp/counterfactual_out\n",
            timeout=120,
        )
        assert counterfactual.exit_code == 0, counterfactual.stderr
        cf_details = sandbox.exec("cat /tmp/counterfactual_out/reward-details.json", timeout=30)
        cf_parsed = json.loads(cf_details.stdout)
        assert cf_parsed["reward"] == 1.0, (
            f"fixture invalid: the hook did not flip the answer — {cf_parsed}"
        )

        # (4) The real, shipped gate ½ + gate 1.
        reward, status = _dry_run_gates(sandbox=sandbox, aux=aux)
        assert reward == 0.0, (reward, status)

        hook_gone = sandbox.exec("test -f /workspace/conftest.py", timeout=15)
        assert hook_gone.exit_code != 0, "gate 1/2 left the planted conftest.py in place"
    finally:
        sandbox.close()


# ---------------------------------------------------------------------------
# Step 3.2 — gate 1/2 does not eat an on-disk solve (.venv exclusions)
# ---------------------------------------------------------------------------


def _write_dummy_plugin_wheel(dest_dir: Path) -> Path:
    """A hand-built, dependency-free wheel whose only interesting content is a
    `conftest.py` — this IS the file the test proves survives gate ½'s
    `git clean`. Built by hand with `zipfile` (no setuptools, no network, no
    dependency on a real PyPI package's install layout) so the assertion
    depends only on `TEST_ROOT_PATHSPECS`, not on some third-party package's
    packaging choices changing out from under this test.
    """
    name, version = "dummy_plugin", "0.0.1"
    dist_info = f"{name}-{version}.dist-info"
    whl_path = dest_dir / f"{name}-{version}-py3-none-any.whl"
    files = {
        f"{name}/__init__.py": "",
        f"{name}/conftest.py": (
            "# planted by tests/test_e2e_env_setup.py — proves the install-\n"
            "# directory exclusions in TEST_ROOT_PATHSPECS survive gate 1/2.\n"
        ),
        f"{dist_info}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: r2e-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record_lines = [f"{path},," for path in files]
    record_lines.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = "\n".join(record_lines) + "\n"
    with zipfile.ZipFile(whl_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, body in files.items():
            zf.writestr(path, body)
    return whl_path


def test_gate_half_preserves_a_venv_installed_conftest() -> None:
    """The regression guard for `TEST_ROOT_PATHSPECS`' install-directory
    exclusions (`:(exclude,glob).venv/**`, `:(exclude,glob)**/site-packages/**`).

    Non-vacuous by construction, not by assumption: the recursive
    `:(glob)**/conftest.py` positive pathspec DOES match a conftest.py nested
    inside `.venv/lib/.../site-packages/`, which is proven directly below via
    a `git clean -n` dry run using ONLY that glob (no excludes) — i.e. without
    the exclusion entries, this exact file would be swept. The repo has no
    `.gitignore` at all, so `.venv` is genuinely untracked-and-unignored, the
    scenario the exclusions exist for.
    """
    dockerfile = _synthetic_python_dockerfile("def test_ok():\n    assert True\n")
    sandbox = EnvSetupSandbox.build_and_start(dockerfile, tag=_unique_tag("gate2"))
    try:
        with tempfile.TemporaryDirectory(prefix="r2e-e2e-envsetup-whl-") as tmp:
            whl = _write_dummy_plugin_wheel(Path(tmp))
            cp = subprocess.run(
                ["docker", "cp", str(whl), f"{sandbox.container_id}:/tmp/{whl.name}"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert cp.returncode == 0, cp.stderr

        setup = sandbox.exec(
            "python3 -m venv /workspace/.venv && "
            "/workspace/.venv/bin/pip install --quiet pytest && "
            f"/workspace/.venv/bin/pip install --no-index --quiet /tmp/{whl.name}",
            timeout=300,
        )
        assert setup.exit_code == 0, setup.stderr

        purelib = sandbox.exec(
            "/workspace/.venv/bin/python -c "
            "\"import sysconfig; print(sysconfig.get_paths()['purelib'])\"",
            timeout=30,
        ).stdout.strip()
        assert purelib, "could not resolve the venv's site-packages directory"
        conftest_abs = f"{purelib}/dummy_plugin/conftest.py"
        conftest_rel = conftest_abs.removeprefix("/workspace/")

        before = sandbox.exec(f"test -f {conftest_abs}", timeout=30)
        assert before.exit_code == 0, "dummy_plugin's conftest.py did not land where expected"

        # Non-vacuousness proof: WITHOUT the exclude entries, the bare
        # recursive glob alone reaches this exact file. Dry run — no `-q`,
        # so the "Would remove ..." lines are actually printed; nothing is
        # deleted, so this does not disturb the real run below.
        unguarded = sandbox.exec(
            "git -C /workspace clean -nd -- ':(glob)**/conftest.py'", timeout=30
        )
        assert conftest_rel in unguarded.stdout, (
            f"fixture invalid: the unguarded glob does not even reach {conftest_rel}\n"
            f"{unguarded.stdout}"
        )

        test_cmd = ". /workspace/.venv/bin/activate && python -m pytest -v tests/test_thing.py"
        aux = build_env_setup_aux_files(
            language="python",
            test_cmds=[test_cmd],
            runner="pytest",
            probe="none",
            base_commit="HEAD",
            package=None,
            dist_name=None,
            f2p=["tests/test_thing.py::test_ok"],
            p2p=[],
        )
        reward, status = _dry_run_gates(sandbox=sandbox, aux=aux)
        assert reward == 1.0, (reward, status)

        after = sandbox.exec(f"test -f {conftest_abs}", timeout=30)
        assert after.exit_code == 0, "gate 1/2 deleted an installed package's conftest.py"
    finally:
        sandbox.close()


# ---------------------------------------------------------------------------
# Step 3.3 — git clean tolerates pathspecs that match nothing
# ---------------------------------------------------------------------------


def test_gate_half_git_clean_tolerates_mostly_unmatched_pathspecs() -> None:
    """The property the SHIPPED, unfiltered `test_roots.json` list depends on
    (RFC 0008 §7f): most repos have `tests/` but not `spec/`, `t/`,
    `tox.ini`, `jest.config.*`, a `.venv`, or `node_modules`. If one
    non-matching pathspec failed the WHOLE `git clean` invocation, the list
    would have to be tree-filtered — which is exactly what would let an
    agent-added `conftest.py` survive in a repo that had none at
    `base_commit` (the property `test_test_root_include_unmatched_config_
    surface` guards at the unit level; this proves the shipped list is safe
    against a REAL git binary, not just against the JSON it's built from).

    Two checks: the literal property (`git clean` on the shipped pathspecs
    exits 0), and — end to end — that the emitted gate ½ agrees
    (`parse_status` never reports `test_restore_failed`) and gate 1 still
    grades the suite correctly afterward.
    """
    dockerfile = _synthetic_python_dockerfile("def test_ok():\n    assert True\n")
    sandbox = EnvSetupSandbox.build_and_start(dockerfile, tag=_unique_tag("gate3"))
    try:
        aux = build_env_setup_aux_files(
            language="python",
            test_cmds=["python -m pytest -v tests/test_thing.py"],
            runner="pytest",
            probe="none",
            base_commit="HEAD",
            package=None,
            dist_name=None,
            f2p=["tests/test_thing.py::test_ok"],
            p2p=[],
        )
        roots = json.loads(aux["tests/test_roots.json"])
        # Sanity on the fixture itself: this repo has ONLY "tests/" among the
        # shipped roots — everything else in the list matches nothing here.
        present = [r for r in roots if r in ("tests/",)]
        assert 0 < len(present) < len(roots), "fixture should mostly NOT match test_roots.json"

        clean = sandbox.exec(
            "git -C /workspace clean -fdq -- " + " ".join(_shell_single_quote(r) for r in roots),
            timeout=60,
        )
        assert clean.exit_code == 0, clean.stderr

        reward, status = _dry_run_gates(sandbox=sandbox, aux=aux)
        assert status != "test_restore_failed", status
        assert reward == 1.0, (reward, status)
    finally:
        sandbox.close()
