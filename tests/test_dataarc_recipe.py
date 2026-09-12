from __future__ import annotations

from collections import Counter

import pytest

from repo2rlenv.pipelines.recipes.dataarc import recipe
from repo2rlenv.spec.options import DataArcOptions


def test_seed_strategy_context_and_lineage(tmp_path):
    (tmp_path / "solution").mkdir()
    (tmp_path / "environment").mkdir()
    (tmp_path / "task.toml").write_text('version = "1.0"')
    (tmp_path / "instruction.md").write_text(
        "# Scheduling\nterminal-bench-canary\n" + "deadline " * 600
    )
    (tmp_path / "solution/solve.sh").write_text("#!/bin/bash\ntrue\n")
    (tmp_path / "environment/Dockerfile").write_text("FROM python:3.12-slim\n")
    rows = recipe.load_seeds(tmp_path, DataArcOptions())
    assert len(rows) == 12
    assert Counter(row["synthetic_strategy"] for row in rows) == {
        "few_shot": 3,
        "self_instruct": 3,
        "evol_instruct": 6,
    }
    assert len({row["title"] for row in rows}) == len(rows)
    assert len({row["parent_bundle_hash"] for row in rows}) == 1
    first = rows[0]
    assert len(first["seed_files"]["instruction.md"]) == 3500
    assert "canary" not in first["seed_files"]["instruction.md"]
    assert "environment/Dockerfile" not in first["seed_files"]
    assert "environment/Dockerfile" in first["environment_context"]
    assert recipe.design(first).seed == first
    for row in rows:
        prompt = recipe.build_prompt(row, "test-model")
        assert "{{" not in prompt
        assert row["source_seed"] in prompt
        assert "test-model" in prompt
    (tmp_path / "solution/solve.sh").write_text("#!/bin/bash\necho changed\n")
    assert (
        recipe.load_seeds(tmp_path, DataArcOptions())[0]["parent_bundle_hash"]
        != first["parent_bundle_hash"]
    )


def test_missing_reference_is_not_an_augmentation_seed(tmp_path):
    (tmp_path / "task.toml").write_text('version = "1.0"')
    with pytest.raises(ValueError, match="reference"):
        recipe.load_seeds(tmp_path, DataArcOptions())
