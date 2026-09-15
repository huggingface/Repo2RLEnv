"""Remote resource contracts are bounded and survive export; no target code runs."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from repo2rlenv.execution import python_repository
from repo2rlenv.pipelines.recipes.repository.export import (
    export_repository_task,
    repository_build,
)
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile
from repo2rlenv.tasksmith.models import Options, Profile
from repo2rlenv.tasksmith.runner import Tasksmith


def profile(**changes):
    return PythonRepositoryProfile(source_paths=["lib"], test_paths=["tests"], **changes)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test_cpus", 0),
        ("test_cpus", 17),
        ("test_cpus", 1.5),
        ("test_cpus", True),
        ("test_memory_mb", 127),
        ("test_memory_mb", 65537),
        ("test_memory_mb", "8192m"),
        ("test_memory_mb", False),
    ],
)
def test_invalid_resource_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        profile(**{field: value})


def fake_docker(monkeypatch, *, state=None):
    calls = []
    state = {"ExitCode": 0, "Running": False, "OOMKilled": False, **(state or {})}

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "cp"] and argv[2].endswith(":/tmp/results.xml"):
            from pathlib import Path

            Path(argv[3]).write_text(
                '<testsuite><testcase classname="core" name="test_value"/></testsuite>'
            )
        stdout = json.dumps([{"State": state}]) if argv[:2] == ["docker", "inspect"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(python_repository, "_run", run)
    return calls, state


@pytest.mark.parametrize(("cpus", "memory"), [(1, 2048), (4, 8192)])
def test_remote_container_resources_keep_offline_execution(tmp_path, monkeypatch, cpus, memory):
    calls, _ = fake_docker(monkeypatch)
    result = python_repository.test_image(
        "fixture-image", profile(test_cpus=cpus, test_memory_mb=memory), tmp_path / "run"
    )
    assert result.returncode == 0
    create = calls[0]
    assert create[create.index("--cpus") + 1] == str(cpus)
    assert create[create.index("--memory") + 1] == f"{memory}m"
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--pids-limit") + 1] == "256"
    assert "--mount" not in create and "--volume" not in create
    assert calls[-1][:3] == ["docker", "rm", "-f"]


def test_oom_has_exact_diagnosis_and_retains_state_before_cleanup(tmp_path, monkeypatch):
    calls, state = fake_docker(monkeypatch, state={"ExitCode": 137, "OOMKilled": True})
    output = tmp_path / "run"
    with pytest.raises(ValueError, match=r"2048 MiB.*OOMKilled=true"):
        python_repository.test_image("fixture-image", profile(), output)
    assert json.loads((output / "state.json").read_text()) == state
    assert not (output / "results.json").exists()
    assert not any(cmd[:2] == ["docker", "cp"] for cmd in calls)
    assert calls[-1][:3] == ["docker", "rm", "-f"]


def test_running_state_does_not_claim_oom_or_test_completion(tmp_path, monkeypatch):
    calls, _ = fake_docker(monkeypatch, state={"Running": True})
    with pytest.raises(ValueError, match="still running"):
        python_repository.test_image("fixture-image", profile(), tmp_path / "run")
    assert calls[-1][:3] == ["docker", "rm", "-f"]


def test_resource_change_keeps_dependency_recipe_but_changes_harbor_contract(tmp_path):
    from harbor.models.task.task import Task

    from repo2rlenv.quality.loop.artifacts import task_identity

    base = tmp_path / "source"
    for name, text in {
        "lib/core.py": "value = 1\n",
        "tests/test_core.py": "private fixture",
    }.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    defaults, enlarged = profile(), profile(test_cpus=4, test_memory_mb=8192)
    assert repository_build(defaults) == repository_build(enlarged)
    hashes = []
    for index, options in enumerate((defaults, enlarged)):
        task = export_repository_task(
            base=base,
            defective={"lib/core.py": b"value = 0\n"},
            reference={"lib/core.py": b"value = 1\n"},
            options=options,
            instruction="Restore the documented public value.",
            destination=tmp_path / f"tasks-{index}",
            name="resource-fixture",
            org="test",
            contrast={"FAIL_TO_PASS": ["test_value"], "PASS_TO_PASS": []},
            metadata={"recipe": "tasksmith", "recipe_version": "1"},
        )
        resources = Task(task).config.verifier.environment
        assert resources.cpus == options.test_cpus
        assert resources.memory_mb == options.test_memory_mb
        hashes.append(task_identity(task))
    assert hashes[0] != hashes[1]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"test_cpus": 5}, "test_cpus=5 exceeds worker allocation 4"),
        ({"test_memory_mb": 16385}, "test_memory_mb=16385 exceeds worker allocation 16384"),
    ],
)
def test_cpu_request_cannot_exceed_known_worker_allocation(changes, message):
    runner = SimpleNamespace(options=Options(worker_cpus=4, worker_memory_mb=16384))
    value = Profile(
        reasoning="Exercise the selected real behavior in an offline container.",
        resource="cpu",
        options=profile(test_selectors=["tests/test_core.py"], **changes),
        dependency_inputs=["setup.py"],
        upstream_test_rationale="The selected case uses a small deterministic local fixture.",
    )
    with pytest.raises(ValueError, match=message):
        Tasksmith._validate_profile_resources(runner, value)
