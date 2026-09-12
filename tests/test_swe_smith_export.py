from __future__ import annotations

import json
import tomllib

import pytest

from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.pipelines.recipes.swe_smith.export import export_candidate
from repo2rlenv.pipelines.recipes.swe_smith.options import SWESmithOptions


@pytest.fixture
def exported(tmp_path):
    generation = tmp_path / "generation"
    base = generation / "base"
    (base / "library").mkdir(parents=True)
    (base / "tests").mkdir()
    (base / "library/value.py").write_text("def value(): return 123456789\n")
    (base / "tests/test_value.py").write_text("assert value() == 123456789\n")
    (base / "LICENSE").write_text("Repository license retained\n")
    evidence = generation / "candidates" / "candidate1"
    evidence.mkdir(parents=True)
    (evidence / "original.py").write_text("def value(): return 123456789\n")
    (evidence / "mutated.py").write_text("def value(): return 0\n")
    candidate = {
        "id": "candidate1",
        "source_file": "library/value.py",
        "repo": "https://github.com/test/library",
        "ref": "abc123",
        "contrast": {"FAIL_TO_PASS": ["tests.test_value::test_value"], "PASS_TO_PASS": []},
    }
    options = SWESmithOptions(source_paths=["library"], test_paths=["tests"])
    return export_candidate(
        generation,
        candidate,
        options,
        "The returned value is wrong.",
        tmp_path / "tasks",
        org="test",
    )


def test_export_separates_reference_and_uses_defective_build_contexts(exported):
    assert inspect_bundle(exported)["integrity_passed"]
    assert not (exported / "environment/source/tests").exists()
    for context in ("environment", "tests"):
        assert (
            exported / context / "source/library/value.py"
        ).read_text() == "def value(): return 0\n"
        assert (exported / context / "source/LICENSE").exists()
    assert "123456789" in (exported / "solution/reference.py").read_text()
    config = tomllib.loads((exported / "task.toml").read_text())
    assert config["verifier"]["environment_mode"] == "separate"
    assert config["environment"]["network_mode"] == "no-network"
    assert config["artifacts"] == [{"source": "/workspace/library/value.py"}]
    assert json.loads((exported / "tests/contract.json").read_text())["expected_passes"]


def test_export_parses_with_pinned_harbor_contract(exported):
    harbor = pytest.importorskip("harbor.models.task.task")
    task = harbor.Task(exported)
    assert task.config.schema_version == "1.3"
    assert task.config.verifier.environment_mode.value == "separate"
    assert task.config.agent.user == "learner"
