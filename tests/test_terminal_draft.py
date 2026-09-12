from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.pipelines.recipes.catalog import get_recipe
from repo2rlenv.pipelines.recipes.terminal.draft import TerminalDraft, emit_draft
from repo2rlenv.pipelines.recipes.terminal.runner import load_seeds


@pytest.fixture
def draft_data():
    return {
        "instruction": "Repair the report generator and preserve its documented output behavior.",
        "environment_setup": "RUN chmod +x /workspace/script.sh",
        "environment_files": [
            {"path": "script.sh", "content": "#!/bin/bash\nexit 1\n", "executable": True}
        ],
        "tests_python": "\n".join(
            f"def test_case_{n}():\n    assert {n} == {n}\n" for n in range(5)
        ),
        "solution_shell": "#!/bin/bash\nset -eu\necho complete\n",
        "weights": [{"name": f"test_case_{n}", "weight": 0.2} for n in range(5)],
        "self_review": "A packaging fixture only, not an execution-valid or quality-accepted task.",
    }


def test_terminal_bundle_keeps_reference_and_tests_out_of_build_context(draft_data, tmp_path):
    draft = TerminalDraft.model_validate(draft_data)
    task = emit_draft(
        draft,
        tmp_path,
        name="fixture",
        org="tests",
        recipe=get_recipe("seta_seed2synth"),
        lineage={"seed_sha256": "a" * 64},
        timeout_sec=60,
    )
    assert inspect_bundle(task)["integrity_passed"]
    assert sorted(path.name for path in (task / "environment").iterdir()) == [
        "Dockerfile",
        "script.sh",
    ]
    assert (task / "tests/grade.py").exists()
    harbor = pytest.importorskip("harbor.models.task.task")
    assert harbor.Task(task).config.environment.network_mode.value == "no-network"


@pytest.mark.parametrize(
    "change", ["traversal", "duplicate", "weight", "empty_collection", "base_override"]
)
def test_inconsistent_drafts_are_rejected_before_cloud_spend(draft_data, change):
    if change == "traversal":
        draft_data["environment_files"][0]["path"] = "../solution/secret"
    elif change == "duplicate":
        draft_data["environment_files"] *= 2
    elif change == "weight":
        draft_data["weights"][0]["weight"] = 0.1
    elif change == "empty_collection":
        draft_data["tests_python"] = "# no tests\n" * 10
    else:
        draft_data["environment_setup"] = "FROM a-different-image"
    with pytest.raises(ValidationError):
        TerminalDraft.model_validate(draft_data)


def test_seed_loader_preserves_input_and_deduplicates_exact_records(tmp_path):
    seed = {
        "source": "unix_linux_se",
        "title": "Shell quoting",
        "question_text": "How should paths containing whitespace be handled?",
    }
    path = tmp_path / "seeds.jsonl"
    path.write_text(json.dumps(seed) + "\n" + json.dumps(seed) + "\n")
    assert load_seeds(path) == [seed]
