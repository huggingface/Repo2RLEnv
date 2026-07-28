"""The OpenEnv export path: dataset parsing, reward contract, and env emission.

Hermetic — no Docker, no network. The Docker-backed sandbox is exercised by the
end-to-end run documented in docs/reference/OPENENV.md, not here.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.emitter.openenv import EmitError, write_openenv_env
from repo2rlenv.openenv.dataset import Repo2RLEnvTask, TaskFormatError, TaskSet
from repo2rlenv.openenv.reward import read_reward

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit(
    dest: Path,
    name: str = "demo__task-1",
    *,
    dockerfile: str | None = "FROM python:3.12-slim\nWORKDIR /workspace\n",
    test_script: str | None = "#!/bin/bash\nexit 0\n",
) -> Path:
    """Write a task with the real Harbor emitter, so tests track the real format."""
    task = HarborTask(
        name=name,
        org="repo2rlenv",
        description="example",
        instruction="# Fix it\n\nmake the tests pass\n",
        oracle_diff="--- a/x.py\n+++ b/x.py\n@@\n-1\n+2\n",
        repo2env={"pipeline": "pr_runtime", "repo": "demo/repo"},
        environment_dockerfile=dockerfile,
        test_script=test_script,
    )
    return write_harbor_task(task, dest)


def _reader(files: dict[str, str]):
    return lambda name: files.get(name)


# ---------------------------------------------------------------------------
# reading an emitted task back
# ---------------------------------------------------------------------------


def test_load_reads_our_own_emitted_task(tmp_path: Path):
    """The reader must accept exactly what the Harbor emitter writes."""
    path = _emit(tmp_path)
    task = Repo2RLEnvTask.load(path)

    # We emit `version`, not Harbor's `schema_version`.
    assert task.schema_version == "1.0"
    assert task.name == "repo2rlenv/demo__task-1"
    assert task.pipeline == "pr_runtime"
    assert task.instruction.startswith("# Fix it")
    assert task.content_hash.startswith("sha256:")
    assert task.dockerfile is not None
    assert task.test_script is not None
    assert task.solve_script is not None
    assert task.oracle_diff is not None
    assert task.runnable


def test_load_rejects_a_non_task_directory(tmp_path: Path):
    with pytest.raises(TaskFormatError):
        Repo2RLEnvTask.load(tmp_path)


def test_timeouts_come_from_task_toml(tmp_path: Path):
    task = Repo2RLEnvTask.load(_emit(tmp_path))
    # The emitter writes agent=1800, verifier=300.
    assert task.agent_timeout_s == 1800.0
    assert task.verifier_timeout_s == 300.0


def test_text_only_task_is_not_runnable(tmp_path: Path):
    """`pr_diff` with emit_harbor_env=False ships no image and no verifier."""
    task = Repo2RLEnvTask.load(_emit(tmp_path, dockerfile=None, test_script=None))
    assert task.dockerfile is None
    assert task.test_script is None
    assert not task.runnable


def test_only_registry_mode_yields_a_pullable_image(tmp_path: Path):
    """local_only / inline_dockerfile image refs may exist on no other machine."""
    path = _emit(tmp_path)
    # The emitter seeds mode=local_only for Dockerfile-bearing tasks.
    assert Repo2RLEnvTask.load(path).repro_mode == "local_only"
    assert Repo2RLEnvTask.load(path).pullable_image is None

    toml_path = path / "task.toml"
    data = tomllib.loads(toml_path.read_text())
    repro = data["metadata"]["repo2env"]["reproducibility"]
    repro["mode"] = "registry"
    repro["image_ref"] = "ghcr.io/acme/task:abc123"
    toml_path.write_text(_dumps(data))

    reloaded = Repo2RLEnvTask.load(path)
    assert reloaded.pullable_image == "ghcr.io/acme/task:abc123"


def _dumps(data: dict) -> str:
    import tomli_w

    return tomli_w.dumps(data)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_taskset_finds_flat_and_nested_layouts(tmp_path: Path):
    """`generate --out` writes flat; `push` stages under tasks/<id>/."""
    _emit(tmp_path, "flat-task")
    _emit(tmp_path / "tasks", "staged-task")

    assert TaskSet(tmp_path).task_ids() == ["flat-task", "tasks/staged-task"]


def test_taskset_accepts_a_single_task_directory(tmp_path: Path):
    path = _emit(tmp_path, "solo")
    assert TaskSet(path).task_ids() == ["solo"]


def test_taskset_resolves_a_bare_name_and_reports_unknown_ids(tmp_path: Path):
    _emit(tmp_path / "tasks", "nested-one")
    task_set = TaskSet(tmp_path)

    assert task_set.get("nested-one").task_id == "tasks/nested-one"
    with pytest.raises(KeyError):
        task_set.get("nope")


# ---------------------------------------------------------------------------
# the reward contract
# ---------------------------------------------------------------------------


def test_reward_txt_is_the_scalar_our_verifiers_write():
    report = read_reward(_reader({"reward.txt": "0.750000\n"}))
    assert report.value == pytest.approx(0.75)
    assert report.source == "reward.txt"
    assert report.graded


def test_reward_json_wins_over_reward_txt():
    report = read_reward(_reader({"reward.json": '{"reward": 0.5}', "reward.txt": "1.0"}))
    assert report.value == pytest.approx(0.5)
    assert report.source == "reward.json"


def test_details_sidecar_is_surfaced_but_never_scored():
    """reward-details.json is diagnostics; the scalar still comes from reward.txt."""
    details = {"resolved": False, "f2p_passed": 0, "f2p_total": 3, "parse_status": "ok"}
    report = read_reward(_reader({"reward.txt": "0.0", "reward-details.json": json.dumps(details)}))
    assert report.value == 0.0
    assert report.source == "reward.txt"
    assert report.details == details
    assert report.as_info()["reward_details"] == details


def test_missing_reward_is_none_not_zero():
    """A fabricated 0.0 is indistinguishable from a real failure."""
    report = read_reward(_reader({}))
    assert report.value is None
    assert not report.graded
    assert report.source == "missing"


def test_unparsable_reward_is_reported_not_guessed():
    report = read_reward(_reader({"reward.txt": "not-a-number"}))
    assert report.value is None
    assert any("not a number" in e for e in report.errors)


# ---------------------------------------------------------------------------
# emitting the environment package
# ---------------------------------------------------------------------------


def test_export_writes_a_deployable_package(tmp_path: Path):
    dataset = tmp_path / "ds"
    _emit(dataset, "demo__task-1")
    dest = tmp_path / "env"

    package = write_openenv_env(dataset, dest, name="Demo Env")

    assert package.task_count == 1
    assert package.runnable_task_count == 1
    assert package.name == "demo-env"
    for rel in ("openenv.yaml", "Dockerfile", "README.md", "pyproject.toml", "server/app.py"):
        assert (dest / rel).is_file(), rel


def test_export_copies_tasks_verbatim(tmp_path: Path):
    """The whole point: serving adds a layer, it does not convert the data."""
    dataset = tmp_path / "ds"
    source = _emit(dataset, "demo__task-1")
    dest = tmp_path / "env"

    write_openenv_env(dataset, dest)

    copied = dest / "tasks" / "demo__task-1"
    for rel in ("task.toml", "instruction.md", "tests/test.sh", "solution/patch.diff"):
        assert (copied / rel).read_text() == (source / rel).read_text(), rel


def test_manifest_matches_the_openenv_schema(tmp_path: Path):
    dataset = tmp_path / "ds"
    _emit(dataset, "demo__task-1")
    dest = tmp_path / "env"

    write_openenv_env(dataset, dest, name="click-env", port=9000)
    manifest = yaml.safe_load((dest / "openenv.yaml").read_text())

    assert manifest["spec_version"] == 1
    assert manifest["name"] == "click-env"
    assert manifest["type"] == "space"
    assert manifest["runtime"] == "fastapi"
    assert manifest["app"] == "server.app:app"
    assert manifest["port"] == 9000
    assert manifest["action"] == "Repo2RLEnvAction"
    assert manifest["observation"] == "Repo2RLEnvObservation"


def test_readme_is_a_space_card_naming_a_real_task(tmp_path: Path):
    dataset = tmp_path / "ds"
    _emit(dataset, "demo__task-1")
    dest = tmp_path / "env"

    write_openenv_env(dataset, dest, name="click-env")
    readme = (dest / "README.md").read_text()

    assert readme.startswith("---\n")
    assert "sdk: docker" in readme
    assert "app_port: 8000" in readme
    # The quickstart must reference a task that actually ships in the package.
    assert 'task_id="demo__task-1"' in readme


def test_dockerfile_wires_the_tasks_dir_and_port(tmp_path: Path):
    dataset = tmp_path / "ds"
    _emit(dataset, "demo__task-1")
    dest = tmp_path / "env"

    write_openenv_env(
        dataset, dest, name="click-env", port=9000, requirement="repo2rlenv[openenv]==9.9.9"
    )
    dockerfile = (dest / "Dockerfile").read_text()

    assert "repo2rlenv[openenv]==9.9.9" in dockerfile
    assert "ENV REPO2RLENV_TASKS_DIR=/app/tasks" in dockerfile
    assert '"--port", "9000"' in dockerfile
    # The server starts task containers on the host daemon.
    assert "/var/run/docker.sock" in dockerfile


def test_export_flags_text_only_tasks_as_unrunnable(tmp_path: Path):
    dataset = tmp_path / "ds"
    _emit(dataset, "runtime-task")
    _emit(dataset, "text-task", dockerfile=None, test_script=None)
    dest = tmp_path / "env"

    package = write_openenv_env(dataset, dest)

    assert package.task_count == 2
    assert package.runnable_task_count == 1
    assert "scored" in (dest / "README.md").read_text()


def test_export_rejects_an_empty_dataset(tmp_path: Path):
    with pytest.raises(EmitError):
        write_openenv_env(tmp_path, tmp_path / "env")


def test_export_is_rerunnable_and_prunes_removed_tasks(tmp_path: Path):
    """Re-exporting must not leave a stale task behind in the package."""
    dataset = tmp_path / "ds"
    _emit(dataset, "keep-me")
    stale = _emit(dataset, "delete-me")
    dest = tmp_path / "env"
    write_openenv_env(dataset, dest)
    assert (dest / "tasks" / "delete-me").is_dir()

    import shutil

    shutil.rmtree(stale)
    package = write_openenv_env(dataset, dest)

    assert package.task_count == 1
    assert not (dest / "tasks" / "delete-me").exists()


def test_emitted_app_module_defers_to_the_library(tmp_path: Path):
    """The package ships config, not generated runtime logic."""
    dataset = tmp_path / "ds"
    _emit(dataset, "demo__task-1")
    dest = tmp_path / "env"

    write_openenv_env(dataset, dest)
    app_py = (dest / "server" / "app.py").read_text()

    assert "from repo2rlenv.openenv import build_app" in app_py
    assert "app = build_app()" in app_py


# ---------------------------------------------------------------------------
# runtime invariants — need the optional `openenv` extra
# ---------------------------------------------------------------------------


def test_dataset_reading_does_not_need_the_openenv_extra():
    """`pip install repo2rlenv` must be enough to read a dataset and export it.

    Guards the lazy __getattr__ in repo2rlenv.openenv: importing the package or
    the emitter must not drag in `openenv`.
    """
    import subprocess
    import sys

    code = (
        "import sys;"
        "import repo2rlenv.openenv as oe;"
        "from repo2rlenv.emitter import write_openenv_env;"
        "oe.TaskSet; oe.Repo2RLEnvTask; oe.read_reward;"
        "print('openenv' in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize("relative", ["../etc/passwd", "../../x", "/tests/test.sh", "a/../../b"])
def test_agent_paths_cannot_escape_the_workspace(relative: str):
    pytest.importorskip("openenv")
    from repo2rlenv.openenv.sandbox import resolve_within

    with pytest.raises(ValueError):
        resolve_within("/workspace", relative)


@pytest.mark.parametrize("relative", ["calc.py", "src/pkg/mod.py", "./a.py"])
def test_agent_paths_inside_the_workspace_resolve(relative: str):
    pytest.importorskip("openenv")
    from repo2rlenv.openenv.sandbox import resolve_within

    assert resolve_within("/workspace", relative).startswith("/workspace")


def test_grading_and_solving_are_not_agent_actions():
    """An agent that could grade or solve on demand would break the boundary."""
    pytest.importorskip("openenv")
    from repo2rlenv.openenv.models import AGENT_ACTIONS, CONTROL_ACTIONS

    assert {"exec", "read", "write"} == AGENT_ACTIONS
    assert {"evaluate", "solve"} == CONTROL_ACTIONS
    assert not (AGENT_ACTIONS & CONTROL_ACTIONS)


def test_action_schema_rejects_unknown_action_types():
    pytest.importorskip("openenv")
    from repo2rlenv.openenv.models import Repo2RLEnvAction

    with pytest.raises(ValueError):
        Repo2RLEnvAction(action_type="rm_rf")


def test_client_round_trips_what_the_server_serializes():
    """The trainer-facing client must parse the server's own step payload."""
    pytest.importorskip("openenv")
    from repo2rlenv.openenv.client import Repo2RLEnvClient
    from repo2rlenv.openenv.models import Repo2RLEnvAction, Repo2RLEnvObservation

    observation = Repo2RLEnvObservation(
        output="ok",
        action_type="evaluate",
        reward=0.75,
        done=True,
        info={"reward_source": "reward.txt"},
    )
    payload = {
        "observation": observation.model_dump(),
        "reward": 0.75,
        "done": True,
    }
    client = Repo2RLEnvClient(base_url="http://localhost:8000")

    result = client._parse_result(payload)
    assert result.reward == pytest.approx(0.75)
    assert result.done is True
    assert result.observation.info["reward_source"] == "reward.txt"

    action = Repo2RLEnvAction(action_type="write", path="a.py", content="x = 1\n")
    assert Repo2RLEnvAction.model_validate(client._step_payload(action)) == action
