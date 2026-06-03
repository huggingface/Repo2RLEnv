"""Tests for ``examples/crabbox/runner.py``.

Two layers:

1. **Unit tests** (always run in CI): assert the right ``crabbox run``
   command line is built for every supported provider, plus
   task.toml / Dockerfile parsing. ``subprocess.run`` is monkeypatched
   so no network or external binary is required.

2. **Live islo smoke** (gated): runs only when ``ISLO_API_KEY`` is set
   *and* ``crabbox`` is on PATH. Pulls a single ``pr_diff`` task from
   the public reference dataset, scores the oracle diff, asserts
   ``final_reward == 1.0``. Skipped in CI by default.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "crabbox"


def _load_runner():
    """Load ``examples/crabbox/runner.py`` as a module without packaging it."""
    spec = importlib.util.spec_from_file_location("crabbox_runner", EXAMPLES_DIR / "runner.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["crabbox_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


runner = _load_runner()


# ─── unit tests ────────────────────────────────────────────────────────────────


def test_provider_config_covers_known_container_providers():
    """The PROVIDER_CONFIG table is the contract — guard regressions."""
    expected = {
        "islo",
        "e2b",
        "modal",
        "daytona",
        "local-container",
        "docker",
        "namespace-devbox",
        "tensorlake",
    }
    assert expected <= set(runner.PROVIDER_CONFIG)
    for name, cfg in runner.PROVIDER_CONFIG.items():
        assert cfg["image_flag"].startswith("--"), name
        assert cfg["workdir_flag"].startswith("--"), name


def test_unsupported_provider_raises_with_helpful_message():
    with pytest.raises(ValueError) as exc_info:
        runner._provider_flags("aws")
    msg = str(exc_info.value)
    assert "aws" in msg
    assert "islo" in msg  # supported providers should be enumerated
    assert "VM providers" in msg  # explains why VMs aren't here


def _make_fake_task(tmp_path: Path) -> Path:
    """Build a minimal pr_diff task tree that satisfies the loader."""
    task_dir = tmp_path / "owner__repo-1"
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "tests").mkdir()
    (task_dir / "solution").mkdir()

    (task_dir / "task.toml").write_text(
        """
version = "1.0"
[task]
name = "default/owner__repo-1"
description = "test fixture"
[metadata]
[metadata.repo2env]
pipeline = "pr_diff"
repo = "owner/repo"
ref = "deadbeefcafe1234"
reference = "https://github.com/owner/repo/pull/1"
source_access = "auto"
reward_kinds = ["diff_similarity_multi_component"]
spec_version = "0.2.0"
[metadata.repo2env.reproducibility]
mode = "local_only"
image_ref = "python:3.12-slim"
""".strip()
    )

    oracle_b64 = base64.b64encode(b"diff --oracle--").decode()
    instr_b64 = base64.b64encode(b"do the thing").decode()
    verifier_b64 = base64.b64encode(b"#!/usr/bin/env python3\nprint('{}')\n").decode()
    (task_dir / "environment" / "Dockerfile").write_text(
        "FROM python:3.12-slim\n"
        "RUN mkdir -p /verifier\n"
        f'RUN echo "{oracle_b64}" | base64 -d > /verifier/oracle.patch\n'
        f'RUN echo "{instr_b64}" | base64 -d > /verifier/instruction.md\n'
        f'RUN echo "{verifier_b64}" | base64 -d > /verifier/verifier.py\n'
    )
    (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\nset -e\necho 'fake test.sh ran'\n")
    (task_dir / "solution" / "patch.diff").write_text("diff --oracle--")
    return task_dir


def test_task_load_extracts_metadata(tmp_path):
    task = runner.Task.load(_make_fake_task(tmp_path))
    assert task.repo == "owner/repo"
    assert task.ref == "deadbeefcafe1234"
    assert task.pipeline == "pr_diff"
    assert task.image_ref == "python:3.12-slim"
    assert task.name == "default/owner__repo-1"


def test_extract_verifier_recovers_three_files(tmp_path):
    task_dir = _make_fake_task(tmp_path)
    out = runner._extract_verifier(task_dir / "environment" / "Dockerfile")
    assert set(out) == {"oracle.patch", "instruction.md", "verifier.py"}
    assert out["oracle.patch"] == b"diff --oracle--"


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Capture every subprocess.run call; the last one is the crabbox invocation.

    Also pre-satisfies ``_preflight``: stubs ``shutil.which('crabbox')`` to a
    fake path and exports a placeholder ``ISLO_API_KEY`` so the runner's
    fail-fast guards don't trip on test hosts that lack either.
    """
    calls: list[SimpleNamespace] = []

    real_run = subprocess.run

    def _fake_run(cmd, *args, **kwargs):
        calls.append(SimpleNamespace(cmd=list(cmd), kwargs=dict(kwargs)))
        # Git invocations during _stage actually need to succeed; defer to real.
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            return real_run(cmd, *args, **kwargs)
        # crabbox: emit a fake stdout that contains the sentinel + a reward.json.
        reward = json.dumps({"final_reward": 0.42, "components": {}})
        stdout = f"some log lines\n{runner.REWARD_SENTINEL}\n{reward}\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=None)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(runner.subprocess, "run", _fake_run)
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/fake/bin/crabbox")
    monkeypatch.setenv("ISLO_API_KEY", "ak_test_placeholder")
    return calls


@pytest.mark.parametrize(
    "provider,image_flag,workdir_flag",
    [
        ("islo", "--islo-image", "--islo-workdir"),
        ("e2b", "--e2b-template", "--e2b-workdir"),
        ("modal", "--modal-image", "--modal-workdir"),
        ("daytona", "--daytona-snapshot", "--daytona-work-root"),
        (
            "local-container",
            "--local-container-image",
            "--local-container-work-root",
        ),
        ("docker", "--local-container-image", "--local-container-work-root"),
        ("namespace-devbox", "--namespace-image", "--namespace-work-root"),
        ("tensorlake", "--tensorlake-image", "--tensorlake-workdir"),
    ],
)
def test_run_task_builds_correct_command_for_each_provider(
    tmp_path, fake_subprocess, provider, image_flag, workdir_flag
):
    task_dir = _make_fake_task(tmp_path)
    result = runner.run_task(task_dir, provider=provider, image="my-image:tag", quiet=True)
    crabbox_call = next(c for c in fake_subprocess if c.cmd and c.cmd[0] == "crabbox")
    argv = crabbox_call.cmd
    assert argv[:4] == ["crabbox", "run", "--provider", provider]
    assert image_flag in argv
    assert argv[argv.index(image_flag) + 1] == "my-image:tag"
    assert workdir_flag in argv
    assert argv[argv.index(workdir_flag) + 1] == "task"
    assert result["final_reward"] == 0.42
    # The wrapper should have written reward.json next to the task.
    written = json.loads((task_dir / "reward.json").read_text())
    assert written["final_reward"] == 0.42


def test_run_task_rejects_unsupported_provider(tmp_path, fake_subprocess):
    with pytest.raises(ValueError, match="not supported"):
        runner.run_task(_make_fake_task(tmp_path), provider="aws", quiet=True)


def test_run_task_writes_keep_flag_when_requested(tmp_path, fake_subprocess):
    runner.run_task(_make_fake_task(tmp_path), keep=True, quiet=True)
    crabbox_call = next(c for c in fake_subprocess if c.cmd and c.cmd[0] == "crabbox")
    assert "--keep" in crabbox_call.cmd


def test_run_task_forwards_allow_env_vars(tmp_path, fake_subprocess):
    runner.run_task(
        _make_fake_task(tmp_path),
        allow_env=["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
        quiet=True,
    )
    argv = next(c for c in fake_subprocess if c.cmd and c.cmd[0] == "crabbox").cmd
    # Each --allow-env should appear immediately followed by its var name.
    flags = [(argv[i], argv[i + 1]) for i, a in enumerate(argv) if a == "--allow-env"]
    assert ("--allow-env", "ANTHROPIC_API_KEY") in flags
    assert ("--allow-env", "OPENAI_API_KEY") in flags


def test_run_task_raises_when_no_sentinel_in_stdout(tmp_path, monkeypatch):
    """If the remote script crashed before printing the sentinel, surface it."""
    real_run = subprocess.run

    def _no_sentinel(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            return real_run(cmd, *args, **kwargs)
        return SimpleNamespace(returncode=1, stdout="boom\n", stderr=None)

    monkeypatch.setattr(subprocess, "run", _no_sentinel)
    monkeypatch.setattr(runner.subprocess, "run", _no_sentinel)
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/fake/bin/crabbox")
    monkeypatch.setenv("ISLO_API_KEY", "ak_test_placeholder")
    with pytest.raises(RuntimeError, match="no reward payload"):
        runner.run_task(_make_fake_task(tmp_path), quiet=True)


def test_preflight_rejects_missing_crabbox(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setenv("ISLO_API_KEY", "ak_test_placeholder")
    with pytest.raises(RuntimeError, match="crabbox CLI not on PATH"):
        runner.run_task(_make_fake_task(tmp_path), quiet=True)


def test_preflight_rejects_missing_islo_key(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/fake/bin/crabbox")
    monkeypatch.delenv("ISLO_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ISLO_API_KEY is not set"):
        runner.run_task(_make_fake_task(tmp_path), quiet=True)


def test_preflight_rejects_missing_task_files(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/fake/bin/crabbox")
    monkeypatch.setenv("ISLO_API_KEY", "ak_test_placeholder")
    task_dir = _make_fake_task(tmp_path)
    (task_dir / "tests" / "test.sh").unlink()
    with pytest.raises(FileNotFoundError, match=r"test\.sh"):
        runner.run_task(task_dir, quiet=True)


def test_run_task_honors_task_image_ref_by_default(tmp_path, fake_subprocess):
    """A task that pins reproducibility.image_ref should run on that image,
    not the hardcoded python:3.12-slim — that was a silent correctness bug."""
    task_dir = _make_fake_task(tmp_path)
    # Patch the task to declare a non-python base image.
    toml = (task_dir / "task.toml").read_text()
    toml = toml.replace('image_ref = "python:3.12-slim"', 'image_ref = "node:20-alpine"')
    (task_dir / "task.toml").write_text(toml)
    runner.run_task(task_dir, quiet=True)  # no --image override
    argv = next(c for c in fake_subprocess if c.cmd and c.cmd[0] == "crabbox").cmd
    # --islo-image (the default provider) should point at the task's declared image.
    assert argv[argv.index("--islo-image") + 1] == "node:20-alpine"


def test_run_task_passes_repo_and_ref_as_positional_args(tmp_path, fake_subprocess):
    """A poisoned task.toml whose repo/ref contain shell metacharacters must not
    inject — repo and ref are passed as positional args, never interpolated."""
    task_dir = _make_fake_task(tmp_path)
    toml = (task_dir / "task.toml").read_text()
    toml = toml.replace('repo = "owner/repo"', 'repo = "evil$(curl x); rm -rf /"').replace(
        'ref = "deadbeefcafe1234"', 'ref = "$(env)"'
    )
    (task_dir / "task.toml").write_text(toml)
    runner.run_task(task_dir, quiet=True)
    argv = next(c for c in fake_subprocess if c.cmd and c.cmd[0] == "crabbox").cmd
    # Sentinel marker for positional args is the lone "_" placeholder for $0.
    assert "_" in argv, f"expected positional-arg marker '_' in argv: {argv}"
    placeholder_idx = argv.index("_")
    assert argv[placeholder_idx + 1] == "evil$(curl x); rm -rf /"
    assert argv[placeholder_idx + 2] == "$(env)"
    # The malicious strings must NOT appear in the bash script body itself.
    script_body = argv[argv.index("bash") + 2]  # bash, -lc, <script>
    assert "evil$(curl x); rm -rf /" not in script_body
    assert "$(env)" not in script_body


# ─── live smoke (gated) ────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("ISLO_API_KEY"),
    reason="ISLO_API_KEY not set — live islo.dev smoke is opt-in",
)
@pytest.mark.skipif(
    not shutil.which("crabbox"),
    reason="crabbox CLI not on PATH — install from openclaw/crabbox",
)
def test_live_islo_oracle_scores_one(tmp_path):
    """Real run on islo.dev: pull a task, score the oracle, expect 1.0.

    Wall time ~50s. Run it with::

        ISLO_API_KEY=... uv run pytest tests/test_examples_crabbox.py \
          -k live_islo -v
    """
    repo2rlenv = shutil.which("repo2rlenv")
    if repo2rlenv is None:
        pytest.skip("repo2rlenv CLI not on PATH")
    dataset = tmp_path / "pr-diff"
    subprocess.run(
        [repo2rlenv, "pull", "AdithyaSK/repo2rlenv-pr-diff", str(dataset)],
        check=True,
    )
    task_dir = dataset / "pallets__click-3466"
    assert task_dir.exists(), "expected pallets__click-3466 in the reference dataset"
    reward = runner.run_task(task_dir, provider="islo", reward_out=tmp_path / "r.json")
    assert reward["final_reward"] == 1.0, reward
