from __future__ import annotations

import json
import tomllib

import pytest

from repo2rlenv.pipelines.recipes.scaler.export import export_task
from repo2rlenv.pipelines.recipes.scaler.families import (
    load_families,
    parse_testcase,
    scale_parameters,
)
from repo2rlenv.pipelines.recipes.scaler.pipeline import ScalerPipeline
from repo2rlenv.spec.input import GenerationInput
from repo2rlenv.spec.recipe_options import ScalerOptions


def family():
    return {
        "name": "Example",
        "logic_description": "Compute the required sum.",
        "generate_testcase": "def generate_testcase(scale): return '1 2', {'a': 1, 'b': 2}",
        "params": {"n": {"base": 2, "min": 3}, "difficulty": {}},
        "difficulty_dict": {"4": 5},
        "solutions": {"solution": ["print(3)"], "language": [3]},
    }


def test_native_scaling_and_tuple_parser():
    assert scale_parameters(family(), 4) == {"n": 13}
    assert parse_testcase("('1 2\\n', {'a': 1, 'b': 2})") == ("1 2\n", {"a": 1, "b": 2})
    assert parse_testcase("'3 4', {\"a\": 3}") == ("3 4", {"a": 3})
    with pytest.raises(ValueError):
        parse_testcase("('1 2', ['wrong'])")


def test_family_controller_requires_no_model_and_hashes_input(tmp_path):
    path = tmp_path / "families.json"
    path.write_text(json.dumps({"example": family()}))
    config = GenerationInput.model_validate(
        {
            "source": {"kind": "family", "path": str(path)},
            "pipeline": {"name": "reasoning_synth", "recipe": "scaler"},
            "execution": {
                "campaign_dir": str(tmp_path),
                "run_id": "fixture",
                "worker_receipt": str(tmp_path / "worker.json"),
                "runtime_wheel": str(tmp_path / "runtime.whl"),
            },
            "output": {
                "destination": str(tmp_path / "out"),
                "org": "test",
                "dataset_name": "scaler",
            },
        }
    )
    pipeline = ScalerPipeline(config, ScalerOptions())
    assert pipeline.worker_configuration(tmp_path)["families"] == {"example": family()}
    assert pipeline.source_identity()["families_sha256"] == load_families(path)[1]


def test_reasoning_export_keeps_reference_private_and_negative_reward(tmp_path):
    candidate = {
        "id": "example",
        "instruction": "Compute the sum and write the answer.",
        "reference_answer": r"\boxed{3}",
        "family": "example",
        "difficulty": 4,
        "actual_seed": 24,
        "source_sha256": "a" * 64,
        "instance_sha256": "b" * 64,
        "reference_code_sha256": "c" * 64,
    }
    task = export_task(candidate, tmp_path, "test")
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["metadata"]["repo2env"]["reward_min"] == -1
    assert config["verifier"]["environment_mode"] == "separate"
    assert json.loads((task / "tests/reference.json").read_text()) == candidate["reference_answer"]
    assert not any(
        candidate["reference_answer"] in p.read_text()
        for p in (task / "environment").rglob("*")
        if p.is_file()
    )
