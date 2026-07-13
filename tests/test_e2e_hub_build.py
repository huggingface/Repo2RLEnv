"""End-to-end Hub→build→oracle smoke test for a published runtime dataset.

Gated on an env var and SKIPPED in normal CI (needs docker + harbor + network
+ several minutes). Run manually:

    R2E_E2E_HUB_BUILD=1 \\
    R2E_E2E_HUB_DATASET=AdithyaSK/repo2rlenv-pr-runtime \\
      uv run pytest tests/test_e2e_hub_build.py -v -s

It proves a consumer can, from scratch:
  1. pull the dataset from the Hub,
  2. build a task's environment/Dockerfile (clean inline recipe — no
     bootstrap image, no registry creds),
  3. run `harbor run -a oracle` and get reward == 1.0 (the gold patch
     resolves the F2P/P2P oracle).

This is the strongest integrity check: it exercises the real published
artifacts (Dockerfile + tests/verifier.py + f2p.json + p2p.json + test.sh)
exactly as a researcher would.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_DATASET = os.environ.get("R2E_E2E_HUB_DATASET", "AdithyaSK/repo2rlenv-pr-runtime")

pytestmark = pytest.mark.skipif(
    os.environ.get("R2E_E2E_HUB_BUILD") != "1",
    reason="set R2E_E2E_HUB_BUILD=1 to run the Hub→build→oracle smoke (needs docker+harbor+network)",
)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def test_hub_task_builds_and_oracle_resolves(tmp_path: Path) -> None:
    if not (_have("docker") and _have("harbor")):
        pytest.skip("docker + harbor required")

    # 1. Pull the published dataset.
    from huggingface_hub import snapshot_download

    snap = Path(
        snapshot_download(repo_id=_DATASET, repo_type="dataset", local_dir=str(tmp_path / "ds"))
    )
    task_dirs = sorted((snap / "tasks").iterdir()) if (snap / "tasks").is_dir() else []
    assert task_dirs, f"no tasks/ in {_DATASET}"

    # Pick one task; assemble a 1-task harbor dataset (tasks live directly under -p).
    one = task_dirs[0]
    ds = tmp_path / "run"
    ds.mkdir()
    shutil.copytree(one, ds / one.name)

    # The published task should be self-rebuildable: clean recipe + plain artifacts.
    df = (ds / one.name / "environment" / "Dockerfile").read_text()
    assert df.lstrip().startswith("FROM ") or "FROM " in df
    assert "base64" not in (ds / one.name / "tests" / "test.sh").read_text()
    assert (ds / one.name / "tests" / "verifier.py").is_file()

    # 2 + 3. harbor run -a oracle → reward 1.0.
    jobs = tmp_path / "jobs"
    env = dict(os.environ)
    proc = subprocess.run(
        [
            "harbor",
            "run",
            "-p",
            str(ds),
            "-a",
            "oracle",
            "--env",
            "docker",
            "-n",
            "1",
            "-y",
            "--quiet",
            "--max-retries",
            "2",
            "--jobs-dir",
            str(jobs),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert proc.returncode == 0, f"harbor run failed: {proc.stderr[-2000:]}"

    rewards = list(jobs.glob("*/*/verifier/reward.txt"))
    assert rewards, "no reward.txt produced"
    val = float(rewards[0].read_text().strip())
    assert val == 1.0, f"oracle reward {val} != 1.0 for {one.name}"

    rj = list(jobs.glob("*/*/verifier/reward-details.json"))
    if rj:
        breakdown = json.loads(rj[0].read_text())
        assert breakdown.get("resolved") is True, breakdown
