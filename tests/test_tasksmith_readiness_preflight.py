"""Path-only checks stop proposed future tests before any remote build/allocation."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from repo2rlenv.spec.recipe_options import PythonRepositoryProfile
from repo2rlenv.tasksmith import native_stages, worker
from repo2rlenv.tasksmith.models import Options, Profile
from repo2rlenv.tasksmith.readiness import validate_readiness_paths


def profile(*selectors, resource="cpu"):
    return Profile(
        reasoning="Use existing upstream tests to validate the pinned dependency environment.",
        resource=resource,
        options=PythonRepositoryProfile(
            source_paths=["src"], test_paths=["tests"], test_selectors=list(selectors)
        ),
        dependency_inputs=["pyproject.toml"],
        upstream_test_rationale="The existing tests are selected before private verifier design.",
    )


@pytest.fixture
def snapshot(tmp_path):
    base = tmp_path / "snapshot"
    (base / "tests").mkdir(parents=True)
    # A preflight must never import the selected file, even to check node names.
    (base / "tests/test_existing.py").write_text("raise RuntimeError('must not be imported')\n")
    return base


@pytest.mark.parametrize(
    "selector",
    [
        "tests",
        "tests/test_existing.py",
        "tests/test_existing.py::test_value",
        "tests/test_existing.py::TestClass::test_value[value::with-colons]",
    ],
)
def test_accepts_existing_directory_file_and_node_prefixes_without_importing(snapshot, selector):
    validate_readiness_paths(snapshot, profile(selector).options)


def test_reports_all_missing_selector_indices_and_exact_prefixes(snapshot):
    options = profile(
        "tests/test_existing.py", "tests/test_future.py::test_barrier", "tests/missing"
    ).options
    with pytest.raises(ValueError) as failure:
        validate_readiness_paths(snapshot, options)
    message = str(failure.value)
    assert "profile.options.test_selectors[1]='tests/test_future.py::test_barrier'" in message
    assert "prefix 'tests/test_future.py'" in message
    assert "profile.options.test_selectors[2]='tests/missing'" in message
    assert "before dependency build or GPU allocation" in message
    assert "Design.additional_tests" in message


def test_fallback_directory_profile_remains_supported(snapshot):
    options = PythonRepositoryProfile(source_paths=["src"], test_paths=["tests"])
    validate_readiness_paths(snapshot, options)
    options.test_paths = ["absent"]
    with pytest.raises(ValueError, match=r"profile.options.test_paths\[0\]='absent'"):
        validate_readiness_paths(snapshot, options)


@pytest.mark.parametrize("parent_link", [False, True])
def test_rejects_symlink_selector_prefixes(snapshot, tmp_path, parent_link):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_external.py").write_text("external = True\n")
    if parent_link:
        (snapshot / "tests/external").symlink_to(outside, target_is_directory=True)
        selector = "tests/external/test_external.py::test_value"
    else:
        (snapshot / "tests/test_link.py").symlink_to(outside / "test_external.py")
        selector = "tests/test_link.py"
    with pytest.raises(ValueError, match="symlink paths are unsupported"):
        validate_readiness_paths(snapshot, profile(selector).options)


@pytest.fixture
def checkout(snapshot):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(snapshot), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q")
    git("add", "tests/test_existing.py")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    head = git("rev-parse", "HEAD")
    # Untracked author-created tests must not appear in the pinned source archive.
    (snapshot / "tests/test_future.py").write_text("def test_future(): pass\n")
    return snapshot, {"repo": "https://github.com/example/fixture", "head": head}


def test_cpu_missing_future_test_fails_before_dependency_or_repository_build(
    checkout, tmp_path, monkeypatch
):
    base, source = checkout

    def forbidden(*args, **kwargs):
        pytest.fail("Readiness failure must precede every Docker/build effect")

    monkeypatch.setattr(worker, "dependency_image", forbidden)
    monkeypatch.setattr(worker, "bootstrap_snapshot", forbidden)
    output = tmp_path / "bootstrap"
    output.mkdir()
    with pytest.raises(ValueError, match=r"test_future\.py"):
        worker.bootstrap(
            source, profile("tests/test_future.py::test_barrier"), output, checkout=base
        )
    assert (base / "tests/test_future.py").exists()
    assert (
        base / "tests/test_existing.py"
    ).read_text() == "raise RuntimeError('must not be imported')\n"
    assert not list(output.iterdir())


def test_cpu_existing_node_reaches_dependency_stage_after_read_only_preflight(
    checkout, tmp_path, monkeypatch
):
    base, source = checkout
    output = tmp_path / "bootstrap"
    output.mkdir()
    archived = []
    materialize = worker.materialize_source

    def observe_snapshot(*args):
        prepared = materialize(*args)
        assert (prepared / "tests/test_existing.py").is_file()
        assert not (prepared / "tests/test_future.py").exists()
        archived.append(prepared)
        return prepared

    monkeypatch.setattr(worker, "materialize_source", observe_snapshot)

    def stop_before_build(*args):
        assert len(archived) == 1 and not archived[0].parent.exists()
        assert not list(output.iterdir())
        raise RuntimeError("dependency boundary reached")

    monkeypatch.setattr(worker, "dependency_image", stop_before_build)
    with pytest.raises(RuntimeError, match="dependency boundary reached"):
        worker.bootstrap(
            source, profile("tests/test_existing.py::test_value"), output, checkout=base
        )
    assert (base / "tests/test_future.py").exists()


def test_native_preparation_rejects_future_test_before_source_reversal(
    checkout, tmp_path, monkeypatch
):
    base, source = checkout
    output = tmp_path / "native-snapshot"
    output.mkdir()
    monkeypatch.setattr(
        worker, "reverse_source", lambda *args: pytest.fail("Do not proceed after failed preflight")
    )
    with pytest.raises(ValueError, match=r"test_future\.py"):
        worker.prepare_native(source, profile("tests/test_future.py", resource="gpu"), base, output)


def test_native_cached_snapshot_is_checked_before_any_gpu_allocation(
    snapshot, tmp_path, monkeypatch
):
    async def forbidden(*args, **kwargs):
        pytest.fail("Missing readiness path must never allocate a GPU")

    monkeypatch.setattr(native_stages, "check_context", forbidden)
    runner = SimpleNamespace(
        options=Options(gpus=2),
        budget=object(),
        remote=lambda *args: {
            "status": "completed",
            "local": str(snapshot.parent),
            "value": {"base_relative": snapshot.name},
        },
    )
    result = native_stages.NativeStages(runner).bootstrap(
        tmp_path / "candidate",
        "bootstrap-1",
        {"id": "fixture"},
        profile("tests/test_future.py", resource="gpu").model_dump(),
        "unused",
    )
    assert result["status"] == "failed"
    assert "profile.options.test_selectors[0]='tests/test_future.py'" in result["error"]
    saved = tmp_path / "candidate/native/bootstrap-1/stage-result.json"
    assert json.loads(saved.read_text()) == result
    assert not (saved.parent / "merged-context").exists()
