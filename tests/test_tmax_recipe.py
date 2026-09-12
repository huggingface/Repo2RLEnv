from __future__ import annotations

import json
import tomllib

import pytest

from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.pipelines.recipes.catalog import get_recipe
from repo2rlenv.pipelines.recipes.terminal.draft import TerminalDraft, emit_draft
from repo2rlenv.pipelines.recipes.tmax.sampler import sample_inputs, template_prompt


def test_taxonomy_sampling_is_repeatable_and_preserves_legacy_axes(tmp_path):
    source = tmp_path / "sampler.json"
    source.write_text(
        json.dumps(
            {
                "seed": 24,
                "count": 20,
                "domains": ["file_operations"],
                "languages": ["Python", "Bash"],
            }
        )
    )
    seeds = sample_inputs(source)
    assert seeds == sample_inputs(source)
    assert len(seeds) == 20
    assert len({json.dumps(item, sort_keys=True) for item in seeds}) == 20
    assert all(item["domain"] == "file_operations" for item in seeds)
    assert all(3 <= len(item["primitive_skills"]) <= 5 for item in seeds)
    assert {item["fixture_kind"] for item in seeds} == {"text_only"}
    assert {item["verifier_kind"] for item in seeds} == {"exact_text"}
    assert "{{domain_label}}" not in template_prompt(seeds[0])
    source.write_text('{"domains": ["misspelled-domain"]}')
    with pytest.raises(ValueError, match="Unknown"):
        sample_inputs(source)


def test_nonroot_terminal_profile_keeps_verifier_root_and_private_files_outside_image(tmp_path):
    draft = TerminalDraft(
        instruction="Create /workspace/output.txt from the supplied input file.",
        environment_setup="",
        environment_files=[{"path": "input.txt", "content": "input data", "executable": False}],
        tests_python="\n".join(
            f"def test_case_{index}():\n    assert True\n" for index in range(5)
        ),
        solution_shell="#!/bin/bash\ncp /workspace/input.txt /workspace/output.txt\n",
        weights=[{"name": f"test_case_{index}", "weight": 0.2} for index in range(5)],
        self_review="Fixture paths and output paths agree.",
    )
    task = emit_draft(
        draft,
        tmp_path / "tasks",
        name="tmax-fixture",
        org="test",
        recipe=get_recipe("tmax"),
        lineage={},
        timeout_sec=60,
        agent_user="user",
    )
    assert inspect_bundle(task)["integrity_passed"]
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["agent"]["user"] == "user"
    assert config["verifier"]["user"] == "root"
    assert not (task / "environment/test_outputs.py").exists()
    assert "chown -R user:user" in (task / "environment/Dockerfile").read_text()
