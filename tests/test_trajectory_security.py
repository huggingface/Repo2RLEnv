"""Regression tests for controller safety, stable outputs and accurate labels."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from repo2rlenv.pipelines.trajectory import gate, task
from repo2rlenv.pipelines.trajectory.assets.grader import grade
from repo2rlenv.pipelines.trajectory.docker import _submission_archive
from repo2rlenv.pipelines.trajectory.families.build import GeneratedFile, keep_only_tests
from repo2rlenv.pipelines.trajectory.families.qa import InputFile
from repo2rlenv.pipelines.trajectory.pipeline import TrajectoryPipeline, convert_sessions
from repo2rlenv.pipelines.trajectory.trajectory import Trajectory
from repo2rlenv.spec.input import GenerationInput
from repo2rlenv.spec.options import TrajectoryOptions
from tests.trajectory_fixtures import environment_files, fixture


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/tmp/file",
        "a/../../file",
        "a/./file",
        "a//file",
        "C:/file",
        "C:\\file",
        "a\\..\\file",
        "file:stream",
        "",
        "a\nfile",
    ],
)
def test_unsafe_model_paths_rejected(tmp_path, path):
    with pytest.raises(ValueError):
        task.write_files(tmp_path / "output", {path: "unsafe"})
    with pytest.raises(ValidationError):
        GeneratedFile(path=path, content="unsafe")
    with pytest.raises(ValidationError):
        InputFile(repo_id="a/b", revision="main", path_in_repo="a.csv", local_name=path)


def test_existing_output_symlink_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    try:
        (output / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        task.write_files(output, {"linked/file": "unsafe"})
    with pytest.raises(ValueError):
        task.write_files(output / "linked", {"file": "unsafe"})
    assert not list(outside.iterdir())


class MinimalFamily(task.Family):
    name = "example"

    def find_candidates(self, trajectory):
        return [1]

    def task_id(self, candidate):
        return "one"

    def environment_files(self, candidate):
        return {"Dockerfile": "FROM scratch"}

    def task_files(self, candidate):
        return {"task.toml": 'version = "1.0"\n'}

    def trials(self, candidate, scratch_dir):
        return [], {}


def test_parallel_duplicate_preserves_accepted_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(task, "build_image", lambda *a: None)
    monkeypatch.setattr(task, "run_trials", lambda *a: {})
    seen = set()

    def run(_):
        return list(task.make_tasks(MinimalFamily(), Trajectory({"steps": []}), tmp_path, seen))

    with ThreadPoolExecutor(4) as pool:
        records = [record for batch in pool.map(run, range(8)) for record in batch]
    assert sum(record["kept"] for record in records) == 1
    assert (tmp_path / "one/task.toml").is_file()
    # A later invocation must not destroy an existing bundle either.
    assert (
        next(task.make_tasks(MinimalFamily(), Trajectory({"steps": []}), tmp_path, set()))["reason"]
        == "already_exists"
    )
    assert (tmp_path / "one/task.toml").is_file()


def test_same_basename_sessions_remain_distinct(tmp_path):
    destination = tmp_path / "converted"
    destination.mkdir()
    for name in ("a", "b"):
        directory = tmp_path / "source" / name
        directory.mkdir(parents=True)
        (directory / "session.jsonl").write_text(json.dumps({"type": "session", "id": name}))
    paths = convert_sessions(tmp_path / "source", "pi", destination, None)
    assert len(set(paths)) == 2
    assert [json.loads(p.read_text())["session_id"] for p in paths] == ["a", "b"]


def test_root_owned_staging_cleanup_preserves_published_task(tmp_path, monkeypatch):
    monkeypatch.setattr(task, "build_image", lambda *a: None)
    monkeypatch.setattr(task, "run_trials", lambda *a: {})
    monkeypatch.setattr(
        task.tempfile.TemporaryDirectory,
        "cleanup",
        lambda self: (_ for _ in ()).throw(PermissionError("root-owned calibration output")),
    )
    removed = []

    def remove(path):
        removed.append(path)
        shutil.rmtree(path)

    monkeypatch.setattr(task, "remove_as_root", remove)
    records = list(task.make_tasks(MinimalFamily(), Trajectory({"steps": []}), tmp_path, set()))
    assert len(records) == 1 and records[0]["kept"]
    assert len(removed) == 1 and removed[0].name.startswith(".trajectory-")
    assert (tmp_path / "one/task.toml").is_file()


def test_bad_input_and_provider_failure_are_reported(tmp_path, monkeypatch):
    from repo2rlenv.pipelines.trajectory.families import qa

    monkeypatch.setattr(
        qa, "ask", lambda *args: (_ for _ in ()).throw(RuntimeError("provider unavailable"))
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "bad.json").write_text('{"schema_version": "ATIF-v1.7"}')
    (source / "good.json").write_text('{"schema_version": "ATIF-v1.7", "steps": []}')
    config = GenerationInput.model_validate(
        {
            "source": {"kind": "trajectory", "path": str(source)},
            "pipeline": {"name": "trajectory_to_env"},
            "output": {"destination": "out", "org": "local", "dataset_name": "test"},
        }
    )
    result = TrajectoryPipeline(config, TrajectoryOptions(scan=False, families=["qa"])).run(
        tmp_path / "out"
    )
    assert result.skip_reasons == {"input_error": 1, "error": 1}
    assert len(json.loads((tmp_path / "out/report.json").read_text())) == 2


@pytest.mark.parametrize("status", ["unknown", "error", None])
def test_scanner_uncertainty_not_clean(tmp_path, monkeypatch, status):
    monkeypatch.setattr(gate.shutil, "which", lambda _: "atif-scan")
    payload = {
        "inputs": [
            {"assessments": [{"id": key, "status": status} for key in gate.BLOCKING_FINDINGS]}
        ]
    }
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, json.dumps(payload)),
    )
    assert gate.scan_trajectory(tmp_path / "trace")[0] is False


@pytest.mark.parametrize(
    ("kind", "gold", "answer", "reward"),
    [
        ("number", "50", "0.5", 0),
        ("number", "50", "50.0", 1),
        ("number", "0.5", "+.5", 1),
        ("number", "50", "about 50", 0),
        ("label", "version 2", "other 2", 0),
        ("list", "a,b", "b,a", 1),
        ("number", "nan", "nan", 0),
    ],
)
def test_qa_typed_answers(kind, gold, answer, reward):
    assert grade(gold, answer, reward_mode="strict_" + kind, abs_tol=0, rel_tol=0).reward == reward


def test_parametrized_functions_survive_filtering():
    source = "import pytest\n@pytest.mark.parametrize('x', [1,2])\ndef test_positive(x):\n    assert x > 0\n"
    assert "def test_positive" in keep_only_tests(source, {"test_positive[1]", "test_positive[2]"})


def test_hidden_test_patch_creates_file_in_empty_repository(tmp_path):
    family, candidate = fixture("build")
    files = family.task_files(candidate)
    script = files["tests/test.sh"]
    patch = script.split("<<'EOF_R2E_TEST_PATCH'\n")[1].split("\nEOF_R2E_TEST_PATCH")[0]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "apply", "-"], cwd=tmp_path, input=patch, text=True, check=True)
    assert (tmp_path / "test_hidden.py").read_text() == files["tests/test_hidden.py"]
    assert "git -C /opt/r2e/pristine rev-parse HEAD" in script
    assert ' - "$R2E_BASE" ' in script


def test_parameterized_grading_selects_only_calibrated_cases():
    from repo2rlenv.pipelines.trajectory.grading import python_verifier_files

    source = "import pytest\n@pytest.mark.parametrize('x', [1,2])\ndef test_value(x):\n    assert x == 1\n"
    files = python_verifier_files(source, {"test_value[1]"}, set())
    assert json.loads(files["tests/f2p.json"]) == ["test_hidden.py::test_value[1]"]
    assert "test_hidden.py::test_value[2]" not in files["tests/test.sh"]


@pytest.mark.parametrize("answer", ["1e999999999", "9" * 257, "NaN", "Infinity", "50 rows"])
def test_malformed_or_extreme_numbers_score_zero(answer):
    assert grade("50", answer, reward_mode="strict_number", abs_tol=0, rel_tol=0).reward == 0


def test_large_exact_numbers_are_not_rounded_to_same_answer():
    assert (
        grade(
            "9007199254740992",
            "9007199254740993",
            reward_mode="strict_number",
            abs_tol=0,
            rel_tol=0,
        ).reward
        == 0
    )


@pytest.mark.parametrize("name", ["repair", "build", "extend", "qa"])
def test_family_contract_and_isolation(tmp_path, name):
    from harbor.models.task.config import TaskConfig

    from repo2rlenv.pipelines.trajectory.pipeline import add_provenance
    from repo2rlenv.validation import validate_task

    family, candidate = fixture(name)
    task.write_files(tmp_path / "environment", environment_files(family, candidate))
    task.write_files(tmp_path, family.task_files(candidate))
    add_provenance(tmp_path, {"family": name, "source_trace": "synthetic"})
    data = tomllib.loads((tmp_path / "task.toml").read_text())
    parsed = TaskConfig.model_validate(data)
    assert parsed.verifier.environment_mode == "separate"
    assert data["artifacts"] == [
        {"source": "/workdir/answer.txt" if name == "qa" else "/workspace"}
    ]
    assert not [f for f in validate_task(tmp_path, data, oracle=True) if f.severity == "error"]


@pytest.mark.parametrize(
    "name", ["../escape", "/workspace/file", "workspace/../escape", "other/file"]
)
def test_artifact_archive_rejects_escape(tmp_path, name):
    source, target = tmp_path / "raw.tar", tmp_path / "safe.tar"
    with tarfile.open(source, "w") as archive:
        archive.addfile(tarfile.TarInfo(name))
    with pytest.raises(ValueError, match="escapes"):
        _submission_archive(source, target, "workspace")


def test_artifact_archive_drops_links_and_privileges(tmp_path):
    source, target = tmp_path / "raw.tar", tmp_path / "safe.tar"
    with tarfile.open(source, "w") as archive:
        link = tarfile.TarInfo("workspace/interpreter")
        link.type, link.linkname = tarfile.SYMTYPE, "/usr/bin/python3"
        archive.addfile(link)
        regular = tarfile.TarInfo("workspace/code.py")
        regular.size, regular.mode, regular.uid = 4, 0o4777, 123
        archive.addfile(regular, io.BytesIO(b"code"))
    _submission_archive(source, target, "workspace")
    with tarfile.open(target) as archive:
        assert archive.getnames() == ["workspace/code.py"]
        entry = archive.getmember("workspace/code.py")
        assert entry.mode == 0o755 and entry.uid == 0
        assert archive.extractfile(entry).read() == b"code"


@pytest.mark.parametrize("output", ["not JSON", "{}", '{"inputs": []}'])
def test_unreadable_scan_is_not_clean(tmp_path, monkeypatch, output):
    monkeypatch.setattr(gate.shutil, "which", lambda _: "atif-scan")
    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, output)
    )
    assert not gate.scan_trajectory(tmp_path / "trace")[0]


@pytest.mark.parametrize("publish", [False, True])
def test_trajectory_cli_proposal_and_publish_routes(tmp_path, monkeypatch, publish):
    from repo2rlenv import cli, hub
    from repo2rlenv.pipelines.base import PipelineResult

    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source.json"
    source.write_text('{"schema_version": "ATIF-v1.7", "steps": []}')
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "source": {"kind": "trajectory", "path": str(source)},
                "pipeline": {
                    "name": "trajectory_to_env",
                    "options": {"propose_only": not publish, "scan": False},
                },
                "output": {
                    "destination": "hf://local/smoke" if publish else str(tmp_path / "out"),
                    "org": "local",
                    "dataset_name": "smoke",
                },
            }
        )
    )
    monkeypatch.setattr(
        TrajectoryPipeline,
        "run",
        lambda self, path: PipelineResult(
            1, int(publish), int(not publish), path, {} if publish else {"proposed": 1}
        ),
    )
    pushed = []

    def push(**kwargs):
        pushed.append(kwargs)
        return SimpleNamespace(
            repo_id="local/smoke", commit_sha="synthetic", registry_url=None, task_count=1
        )

    monkeypatch.setattr(hub, "push_to_hub", push)
    assert cli.main(["--no-ui", "generate", "--config", str(config)]) == 0
    assert len(pushed) == int(publish)
    if publish:
        assert pushed[0]["repo_source"] is None
