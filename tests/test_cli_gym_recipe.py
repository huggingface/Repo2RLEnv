from __future__ import annotations

import json
import tomllib

from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.pipelines.recipes.cli_gym.export import export_task
from repo2rlenv.pipelines.recipes.cli_gym.models import Inversion
from repo2rlenv.spec.recipe_options import EnvironmentRepairOptions


def test_inversion_is_build_only_and_original_tests_are_retained(tmp_path):
    base = tmp_path / "source"
    (base / "tests").mkdir(parents=True)
    (base / "module.py").write_text("value = 3\n")
    (base / "tests/test_module.py").write_text("def test_value():\n    assert True\n")
    inversion = Inversion(
        destruction_shell="#!/bin/bash\nset -eu\nprintf broken > /etc/example-setting\n",
        recovery_shell="#!/bin/bash\nset -eu\nrm /etc/example-setting\n",
        explanation="Change an environment setting and restore its original absent state.",
    )
    task = export_task(
        base=base,
        destination=tmp_path / "out",
        name="cli-fixture",
        instruction="Restore the environment so the existing test suite passes without changing source or tests. Inspect the current setup and repair the failure.",
        inversion=inversion,
        options=EnvironmentRepairOptions(source_paths=["module.py"], test_paths=["tests"]),
        org="test",
        lineage={},
        required_tests=["tests.test_module::test_value"],
        protected={"module.py": "a" * 64},
        resume=False,
    )
    assert inspect_bundle(task)["integrity_passed"]
    dockerfile = (task / "environment/Dockerfile").read_text()
    assert "COPY source/ /workspace/" in dockerfile
    assert "COPY . " not in dockerfile
    assert "/etc/example-setting" in dockerfile
    assert not any(
        "example-setting" in file.read_text()
        for file in (task / "environment/source").rglob("*")
        if file.is_file()
    )
    contract = json.loads((task / "tests/contract.json").read_text())
    assert contract["required_tests"] == ["tests.test_module::test_value"]
    assert (task / "tests/repository-tests/test_module.py").is_file()
    assert tomllib.loads((task / "task.toml").read_text())["verifier"]["user"] == "root"
