"""Exercise actual Harbor artifact transfer and separate grading, without an LLM.

Opt in with R2E_HARBOR_TESTS=1. R2E_HARBOR_PROVIDER selects docker (default),
modal or daytona. Cloud images are built by the provider, never local Docker.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
import tomli_w

from tests.test_separate_verifier_container import FIXED, RUNTEST_PATCH, _build_task

pytestmark = pytest.mark.skipif(
    os.environ.get("R2E_HARBOR_TESTS") != "1",
    reason="set R2E_HARBOR_TESTS=1 to run real Harbor environments",
)


@pytest.fixture(scope="module")
def harbor_task_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("harbor-template")
    _build_task(root, "unused", "python:3.12", build_image=False)
    environment = root / "environment"
    environment.mkdir()
    shutil.move(root / "repo", environment / "repo")
    shutil.move(root / "Dockerfile", environment / "Dockerfile")
    (root / "instruction.md").write_text("Fix clamp so its result stays within the bounds.\n")
    (root / "solution").mkdir()
    (root / "task.toml").write_text(
        tomli_w.dumps(
            {
                "version": "1.0",
                "environment": {
                    "cpus": 1,
                    "memory_mb": 1024,
                    "storage_mb": 4096,
                    "build_timeout_sec": 300,
                },
                "agent": {"timeout_sec": 30},
                "verifier": {
                    "timeout_sec": 90,
                    "environment_mode": "separate",
                    "user": "root",
                },
                "artifacts": [{"source": "/workspace"}],
            }
        )
    )
    return root


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("submission", ["oracle", "nop", "conftest", "in_process"])
async def test_harbor_separate_grading(harbor_task_template: Path, tmp_path: Path, submission):
    pytest.importorskip("harbor")
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial

    provider = os.environ.get("R2E_HARBOR_PROVIDER", "docker")
    if provider not in {"docker", "modal", "daytona"}:
        pytest.fail(f"Unsupported smoke-test provider: {provider}")
    task = tmp_path / "task"
    shutil.copytree(harbor_task_template, task)
    if submission == "oracle":
        source = FIXED
        target = "/workspace/pkg/clamp.py"
    elif submission == "in_process":
        source = RUNTEST_PATCH
        target = "/workspace/pkg/clamp.py"
    elif submission == "conftest":
        source = (
            "import pytest\n"
            "@pytest.hookimpl(hookwrapper=True)\n"
            "def pytest_runtest_makereport(item, call):\n"
            "    outcome = yield\n"
            "    outcome.get_result().outcome = 'passed'\n"
        )
        target = "/workspace/conftest.py"
    else:
        source = ""
        target = "/workspace/noop.txt"
    (task / "solution" / "solve.sh").write_text(
        f"#!/bin/bash\nset -eu\ncat > {target} <<'R2E_SUBMISSION'\n{source}\nR2E_SUBMISSION\n"
    )
    config = TrialConfig.model_validate(
        {
            "task": {"path": task},
            "trial_name": f"{provider}-{submission}",
            "trials_dir": tmp_path / "trials",
            "agent": {"name": "nop" if submission == "nop" else "oracle"},
            "environment": {"type": provider, "delete": True},
        }
    )
    trial = await Trial.create(config)
    result = await asyncio.wait_for(trial.run(), timeout=660)
    assert result.exception_info is None, result.exception_info
    assert result.verifier_result is not None
    assert result.verifier_result.rewards == {"reward": 1.0 if submission == "oracle" else 0.0}
    assert (trial.paths.verifier_dir / "submission.json").is_file()
    if submission == "in_process":
        import json

        details = json.loads((trial.paths.verifier_dir / "reward-details.json").read_text())
        assert details["tamper_check"] == "tripped"
