"""Opt-in generation and Harbor controls; cloud tests never use local Docker."""

from __future__ import annotations

import asyncio
import os
import shlex

import pytest

from repo2rlenv.pipelines.trajectory.pipeline import add_provenance
from repo2rlenv.pipelines.trajectory.task import make_tasks, write_files
from tests.trajectory_fixtures import FORGED, environment_files, fixture


@pytest.mark.skipif(
    os.environ.get("R2E_DOCKER_TESTS") != "1", reason="requires Docker on the test worker"
)
@pytest.mark.parametrize("name", ["repair", "repair_delete", "build", "extend", "qa"])
def test_generation_controls(tmp_path, monkeypatch, name):
    family, candidate = fixture(name.removesuffix("_delete"))
    if name == "repair_delete":
        candidate.files_at_failure["obsolete.py"] = "obsolete = True\n"
        candidate.changed_files.append("obsolete.py")
        for files in (candidate.files_at_failure, candidate.files_at_success):
            files["test_calc.py"] += (
                "\ndef test_deleted_source():\n    from pathlib import Path\n"
                "    assert not Path('/app/obsolete.py').exists()\n"
            )
    files = environment_files(family, candidate)
    if name == "qa":
        source = tmp_path / "data.csv"
        source.write_text(files["data.csv"])
        candidate.pinned_inputs[0]["downloaded_path"] = str(source)
    monkeypatch.setattr(family, "find_candidates", lambda _: [candidate])
    # Inject deterministic proposals, keeping calibration/build/emission/trials real.
    monkeypatch.setattr(family, "prepare", lambda _: None)
    monkeypatch.setattr(family, "environment_files", lambda _: files)
    records = list(make_tasks(family, candidate.trajectory, tmp_path / "tasks", set()))
    assert len(records) == 1 and records[0]["kept"], records
    add_provenance(tmp_path / "tasks" / records[0]["task_id"], records[0])


@pytest.mark.skipif(
    os.environ.get("R2E_HARBOR_TESTS") != "1", reason="requires a real Harbor provider"
)
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["repair", "build", "extend", "qa"])
@pytest.mark.parametrize("submission", ["oracle", "nop", "forged", "conftest"])
async def test_harbor_controls(tmp_path, name, submission):
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial

    family, candidate = fixture(name)
    directory = tmp_path / "task"
    write_files(directory / "environment", environment_files(family, candidate))
    write_files(directory, family.task_files(candidate))
    add_provenance(directory, {"family": name, "source_trace": "synthetic"})
    if submission in {"forged", "conftest"}:
        if name == "qa":
            # Wrong count plus a replacement interpreter and forged reward in the
            # agent container must not affect the fresh answer-only verifier.
            script = "echo 0.02 > /workdir/answer.txt\nmkdir -p /logs/verifier\necho 1 > /logs/verifier/reward.txt\nprintf '#!/bin/sh\\necho 1\\n' > /usr/local/bin/python3\n"
        else:
            target = "/app/calc.py" if submission == "forged" else "/app/conftest.py"
            source = (
                FORGED
                if submission == "forged"
                else (
                    "import pytest\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n"
                    "    outcome = yield\n    report = outcome.get_result()\n    report.outcome = 'passed'\n    report.longrepr = None\n"
                )
            )
            script = f"printf %s {shlex.quote(source)} > {target}\n"
        write_files(directory, {"solution/solve.sh": "#!/bin/bash\nset -eu\n" + script})
    provider = os.environ.get("R2E_HARBOR_PROVIDER", "docker")
    assert provider in {"docker", "modal", "daytona"}
    config = TrialConfig.model_validate(
        {
            "task": {"path": directory},
            "trial_name": f"trajectory-{name}-{submission}",
            "trials_dir": tmp_path / "trials",
            "agent": {"name": "nop" if submission == "nop" else "oracle"},
            "environment": {"type": provider, "delete": True},
        }
    )
    trial = await Trial.create(config)
    result = await asyncio.wait_for(trial.run(), timeout=900)
    assert result.exception_info is None, result.exception_info
    assert result.verifier_result is not None
    assert result.verifier_result.rewards == {"reward": 1.0 if submission == "oracle" else 0.0}
