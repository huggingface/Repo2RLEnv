from __future__ import annotations

import pytest

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.pipelines.recipes.seta_evol.recipe import load_parents
from repo2rlenv.spec.recipe_options import TaskEvolutionOptions


def test_evolution_preserves_parent_identity_and_separates_variants(tmp_path):
    parent = write_bundle(
        TaskBundle(
            name="parent",
            org="tests",
            instruction="Restore correct output formatting.",
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/bash\ntrue\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/bash\nfalse\n", executable=True),
            },
            metadata={
                "recipe": "fixture",
                "recipe_version": "1",
                "reward_kinds": ["test_execution"],
            },
        ),
        tmp_path,
    )
    before = {str(path): path.read_bytes() for path in parent.rglob("*") if path.is_file()}
    records = load_parents(parent, TaskEvolutionOptions(variants_per_parent=3))
    assert len(records) == 3
    assert len({item["evolution_strategy"] for item in records}) == 3
    assert len({item["parent_bundle_hash"] for item in records}) == 1
    assert all("solution/solve.sh" in item["parent_files"] for item in records)
    assert before == {str(path): path.read_bytes() for path in parent.rglob("*") if path.is_file()}
    (parent / "instruction.md").write_text("Changed after verification")
    with pytest.raises(ValueError, match="changed"):
        load_parents(parent, TaskEvolutionOptions())


def test_unknown_evolution_strategy_is_rejected():
    with pytest.raises(ValueError, match="Input should be"):
        TaskEvolutionOptions(strategies=["rename_files_only"])
