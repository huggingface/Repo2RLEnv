"""GPU construction contracts with provider execution mocked at its boundary."""

import json
from types import SimpleNamespace

import pytest

from repo2rlenv.quality.test_results import TestResults
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile
from repo2rlenv.tasksmith.models import Design, Options, Profile
from repo2rlenv.tasksmith.native_stages import NativeStages


def test_gpu_selection_never_falls_back_to_an_unvalidated_provider():
    assert Options(gpus=2).gpus == 2
    with pytest.raises(ValueError, match="native Modal"):
        Options(provider="daytona", gpus=1)
    with pytest.raises(ValueError):
        Options(gpus=3)


@pytest.mark.parametrize(
    ("source", "excluded"),
    [(["src", "src/model"], []), (["src"], ["src/answer.md"])],
)
def test_profile_rejects_invalid_submission_boundaries_before_bootstrap(source, excluded):
    with pytest.raises(ValueError, match="Source roots must be disjoint"):
        Profile(
            reasoning="Inspect the pinned source and exercise local behavior.",
            resource="cpu",
            options=PythonRepositoryProfile(
                source_paths=source,
                public_exclude=excluded,
                test_paths=["tests"],
                test_selectors=["tests/test_api.py"],
            ),
            dependency_inputs=["setup.py"],
            upstream_test_rationale="The selected tests use small offline fixtures.",
        )


def test_construct_runs_both_gpu_contrasts_and_reuses_only_identical_inputs(tmp_path, monkeypatch):
    from harbor.models.task.task import Task

    from repo2rlenv.tasksmith import native_stages

    base = tmp_path / "base"
    for name, text in {
        "lib/core.py": "value = 1\n",
        "tests/test_core.py": "def test_new():\n    assert True\n\ndef test_old():\n    assert True\n",
    }.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    defective = tmp_path / "defective/lib"
    defective.mkdir(parents=True)
    (defective / "core.py").write_text("value = 0\n")
    options = PythonRepositoryProfile(
        source_paths=["lib"],
        test_paths=["tests"],
        test_selectors=["tests/test_core.py"],
        use_system_site_packages=True,
    )
    profile = Profile(
        reasoning="Execute the actual CUDA feature on two local devices.",
        resource="gpu",
        options=options,
        dependency_inputs=["setup.py"],
        upstream_test_rationale="The selected tests cover public GPU behavior offline.",
    )
    design = Design(
        instruction="Correct the public computation on CUDA tensors while preserving their device and the existing neighboring API behavior.",
        requirements=[
            {
                "behavior": "Compute the correct tensor values.",
                "verification": "Compare independent values and gradients.",
                "source_evidence": "Pinned production patch.",
            }
        ],
        verifier_rationale="Test independent numerical outcomes on the selected GPUs.",
        wrong_solution_ideas=["Return an unchanged tensor."],
        valid_alternative_ideas=["Use an equivalent sum."],
    )
    source = {
        "id": "fixture",
        "source_files": ["lib/core.py"],
        "url": "https://example.org/pull/1",
        "head": "h",
        "base": "b",
        "source_diff": "diff",
        "workspace_strategy": "head_minus_source_patch",
    }
    ready = {"base": str(base), "defective": str(defective.parent), "removed": []}
    seen = []

    async def check(context, profile, output, budget, gpus, **kwargs):
        assert gpus == 2
        broken = output.name == "defective"
        assert (context / "source/lib/core.py").read_text() == (
            "value = 0\n" if broken else "value = 1\n"
        )
        seen.append(output.name)
        return TestResults(
            {
                "tests.test_core::test_new": "failed" if broken else "passed",
                "tests.test_core::test_old": "passed",
            },
            int(broken),
        )

    monkeypatch.setattr(native_stages, "check_context", check)
    runner = SimpleNamespace(options=Options(gpus=2), budget=object())
    stages = NativeStages(runner)
    args = (
        tmp_path / "candidate",
        "construct-1",
        source,
        profile.model_dump(),
        design.model_dump(),
        ready,
    )
    result = stages.construct(*args)
    assert result["status"] == "completed", result
    assert seen == ["healthy", "defective"]
    task = Task(tmp_path / "candidate/native/construct-1" / result["value"]["task_relative"])
    assert task.config.environment.gpus == task.config.verifier.environment.gpus == 2
    assert task.config.verifier.environment_mode.value == "separate"
    assert len(task.config.artifacts) == 1
    assert task.config.artifacts[0].source == "/workspace/lib"
    contract = json.loads((task.task_dir / "tests/contract.json").read_text())
    assert contract["submitted_roots"] == ["lib"]
    assert contract["submitted_files"] == ["lib/core.py"]
    assert stages.construct(*args) == result
    assert len(seen) == 2
    changed = design.model_dump()
    changed["instruction"] += " Changed requirement."
    with pytest.raises(ValueError, match="inputs changed"):
        stages.construct(*args[:4], changed, ready)


def test_unknown_allocation_is_not_fed_back_as_a_verifier_repair(tmp_path):
    path = tmp_path / "check/allocations/receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"state": "creation_uncertain"}))
    with pytest.raises(RuntimeError, match="Reconcile native allocation"):
        NativeStages.failure(tmp_path, ConnectionError("lost response"))


@pytest.mark.parametrize("failed", [False, True])
def test_native_snapshot_transfer_preserves_profile_failure(tmp_path, monkeypatch, failed):
    from repo2rlenv.execution.artifacts import unpack_evidence
    from repo2rlenv.tasksmith import worker

    config = tmp_path / "request.json"
    config.write_text(
        json.dumps({"stage": "prepare_native", "source": {}, "profile": {}, "checkout": "unused"})
    )
    output = tmp_path / "artifact"
    monkeypatch.setenv("REPO2RLENV_REMOTE_WORKER", "1")
    monkeypatch.setattr("sys.argv", ["worker", str(config), str(output)])
    monkeypatch.setattr(worker.Profile, "model_validate", lambda value: value)

    def prepare(source, profile, checkout, destination):
        snapshot = destination / "snapshot"
        snapshot.mkdir()
        (snapshot / "module.py").write_text("value = 1\n")
        if failed:
            (snapshot / "unhandled.md").symlink_to("module.py")
            raise ValueError("Snapshot has a link requiring support: unhandled.md")
        return {"base_relative": "snapshot"}

    monkeypatch.setattr(worker, "prepare_native", prepare)
    worker.main()
    received = unpack_evidence(
        output.with_suffix(".tar.gz"), tmp_path / "download", root_name="artifact"
    )
    result = json.loads((received / "stage-result.json").read_text())
    assert result["status"] == ("failed" if failed else "completed")
    if failed:
        assert "unhandled.md" in result["error"]
        assert (received / "traceback.txt").is_file()
        assert not (received / "snapshot").exists()
        assert (output / "snapshot/unhandled.md").is_symlink()
    else:
        assert (received / "snapshot/module.py").read_text() == "value = 1\n"
