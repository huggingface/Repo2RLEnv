"""Manifest registration preserves the existing verifier contract exactly."""

from __future__ import annotations

import json
import tomllib

import pytest

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.artifacts import apply_repair, refresh_identity, task_identity
from repo2rlenv.quality.loop.models import Edit, Repair


@pytest.fixture
def task_factory(tmp_path):
    def create(contract):
        files = {
            "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
            "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
            "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
        }
        if contract is not None:
            files["tests/contract.json"] = TaskFile.text(contract)
        return write_bundle(
            TaskBundle(
                name="case-registration",
                org="tests",
                instruction="Implement the requested behavior.\n",
                files=files,
                metadata={
                    "recipe": "test",
                    "recipe_version": "1",
                    "reward_kinds": ["test_execution"],
                },
            ),
            tmp_path / "tasks",
        )

    return create


def repair(ids, **kwargs):
    return Repair(
        explanation="Register additional independently specified behavior checks.",
        addressed_issues=["New tests are not registered in the reward manifest."],
        edits=[],
        append_expected_passes=ids,
        **kwargs,
    )


def test_append_preserves_old_order_fields_original_and_creates_unverified_revision(
    task_factory, tmp_path
):
    contract = {
        "expected_passes": ["tests/test_api.py::test_z", "tests/test_api.py::test_a"],
        "submitted_files": ["src/package.py"],
        "immutable_assets": {"reference.txt": "exact-original-hash"},
        "other": {"ordered": [3, 1, 2], "value": False},
    }
    original_text = json.dumps(contract)
    task = task_factory(original_text)
    original_hash = task_identity(task)
    additions = ["tests/test_api.py::test_new[second]", "tests/test_api.py::test_new[first]"]
    destination = apply_repair(task, repair(additions), tmp_path / "r1" / task.name)
    updated = json.loads((destination / "tests/contract.json").read_text())
    assert updated == {**contract, "expected_passes": [*contract["expected_passes"], *additions]}
    assert (task / "tests/contract.json").read_text() == original_text
    assert task_identity(task) == original_hash != task_identity(destination)
    label = tomllib.loads((destination / "task.toml").read_text())["metadata"]["repo2env"][
        "evaluation"
    ]
    assert label["status"] == "unverified"
    assert label["reason_codes"] == ["task_changed"]
    receipt = json.loads((destination.parent / "repair.json").read_text())
    assert receipt["parent_hash"] == original_hash
    assert receipt["bundle_hash"] == task_identity(destination)
    assert receipt["repair"]["append_expected_passes"] == additions


@pytest.mark.parametrize("ids", [[""], [" \n"], ["case", "case"], [12], [None]])
def test_invalid_append_ids_are_rejected(ids):
    with pytest.raises(ValueError):
        repair(ids)


@pytest.mark.parametrize(
    "contract",
    [
        None,
        "{malformed",
        "[]",
        "{}",
        '{"expected_passes": "case"}',
        '{"expected_passes": [1]}',
        '{"expected_passes": [""]}',
        '{"expected_passes": ["case", "case"]}',
        '{"expected_passes": ["new"]}',
        '{"expected_passes": ["old"], "expected_passes": ["replacement"]}',
        '{"expected_passes": ["old"], "other": {"value": 1, "value": 2}}',
    ],
)
def test_invalid_or_absent_contract_and_existing_ids_are_not_repaired_implicitly(
    task_factory, tmp_path, contract
):
    task = task_factory(contract)
    before = task_identity(task)
    destination = tmp_path / "r1" / task.name
    with pytest.raises((ValueError, FileNotFoundError)):
        apply_repair(task, repair(["new"]), destination)
    assert task_identity(task) == before
    assert not destination.exists()
    assert not (destination.parent / "repair.json").exists()


def test_simultaneous_text_edit_of_contract_is_rejected(task_factory, tmp_path):
    task = task_factory('{"expected_passes": ["existing"]}')
    proposal = repair(["new"]).model_copy(
        update={
            "edits": [
                Edit(
                    path="tests/contract.json",
                    old="existing",
                    new="replacement",
                    executable=False,
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="Cannot append expected passes and text-edit"):
        apply_repair(task, proposal, tmp_path / "r1" / task.name)


def test_contract_directory_is_a_bounded_validation_failure(task_factory, tmp_path):
    task = task_factory(None)
    path = task / "tests/contract.json"
    path.mkdir()
    (path / "unrelated.txt").write_text("not a JSON contract")
    refresh_identity(task)
    with pytest.raises(ValueError, match=r"requires an existing tests/contract\.json file"):
        apply_repair(task, repair(["new"]), tmp_path / "r1" / task.name)


def test_unrelated_legacy_repair_defaults_to_no_append():
    proposal = Repair.model_validate(
        {
            "explanation": "Clarify the public contract.",
            "addressed_issues": ["Ambiguous instruction"],
            "edits": [{"path": "instruction.md", "old": "old", "new": "new", "executable": False}],
        }
    )
    assert proposal.append_expected_passes == []
