from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.contracts import bind_contracts
from repo2rlenv.tasksmith.models import ArtifactRef, digest
from repo2rlenv.tasksmith.trials import TrialOutcome
from repo2rlenv.tasksmith.validation import paired_witness


@pytest.fixture
def witness_pair(tmp_path):
    """Retained controller observations only; no submitted program is executed."""
    outcomes = []
    for name, reward, value, source_hash in (
        ("baseline", 0, [1, 1], "a" * 64),
        ("oracle", 1, [1, 2], "b" * 64),
    ):
        folder = tmp_path / name / "verifier"
        folder.mkdir(parents=True)
        (folder / "collection.json").write_text(
            json.dumps({"src/example/__init__.py": {"sha256": source_hash, "size_bytes": 20}})
        )
        outcomes.append(
            TrialOutcome(
                kind=name,
                path=str(folder.parent),
                task_digest="c" * 64,
                provider="modal",
                profile_digest="d" * 64,
                materialization_digest="e" * 64,
                observed_reward=reward,
                reward=reward,
                collection_verified=True,
                cleanup_confirmed=True,
                source_observation={
                    "value": value,
                    "package_origin": "/workspace/src/example/__init__.py",
                    "origin_sha256": source_hash,
                },
            )
        )
    return outcomes


def test_paired_witness_binds_behavior_source_and_profile_without_rewriting_trials(witness_pair):
    before = copy.deepcopy([row.model_dump() for row in witness_pair])
    result = paired_witness(*witness_pair)
    assert result["passed"] is True
    assert result["reasons"] == []
    assert result["changed_source"] == ["src/example/__init__.py"]
    assert result["profile_digest"] == witness_pair[0].profile_digest
    assert [row.model_dump() for row in witness_pair] == before
    assert all(not row.materialization_verified for row in witness_pair)


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_digest", "f" * 64),
        ("provider", "daytona"),
        ("profile_digest", "f" * 64),
        ("materialization_digest", "f" * 64),
    ],
)
def test_paired_witness_rejects_cross_profile_comparisons(witness_pair, field, value):
    baseline, oracle = witness_pair
    result = paired_witness(baseline, replace(oracle, **{field: value}))
    assert result["passed"] is False
    assert any("different task/profile" in reason for reason in result["reasons"])


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "change",
    [
        {"origin_sha256": "f" * 64},
        {"package_origin": "/usr/local/lib/python3.12/site-packages/example/__init__.py"},
        {"package_origin": "/workspace/src/example/not-collected.py"},
    ],
)
def test_paired_witness_requires_observed_origin_in_exact_transferred_inventory(
    witness_pair, index, change
):
    row = witness_pair[index]
    witness_pair[index] = replace(row, source_observation={**row.source_observation, **change})
    result = paired_witness(*witness_pair)
    assert result["passed"] is False
    assert any("does not match transferred bytes" in reason for reason in result["reasons"])


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "change",
    [
        {"cleanup_confirmed": False},
        {"collection_verified": False},
        {"error": "Provider cleanup unresolved"},
        {"reward": None},
        {"source_observation": None},
    ],
)
def test_paired_witness_does_not_accept_partial_remote_evidence(witness_pair, index, change):
    witness_pair[index] = replace(witness_pair[index], **change)
    result = paired_witness(*witness_pair)
    assert result["passed"] is False
    assert result["reasons"]


def test_paired_witness_requires_changed_observation_even_with_changed_files(witness_pair):
    baseline, oracle = witness_pair
    oracle = replace(
        oracle,
        source_observation={
            **oracle.source_observation,
            "value": baseline.source_observation["value"],
        },
    )
    result = paired_witness(baseline, oracle)
    assert result["passed"] is False
    assert "Probe did not witness a change in the PR's behavior" in result["reasons"]
    assert result["changed_source"]


def test_paired_witness_requires_changed_files_even_with_changed_observation(witness_pair):
    baseline, oracle = witness_pair
    relative = "verifier/collection.json"
    (Path(oracle.path) / relative).write_bytes((Path(baseline.path) / relative).read_bytes())
    oracle = replace(
        oracle,
        source_observation={
            **oracle.source_observation,
            "origin_sha256": baseline.source_observation["origin_sha256"],
        },
    )
    result = paired_witness(baseline, oracle)
    assert result["passed"] is False
    assert result["changed_source"] == []
    assert "Reference did not change collected source" in result["reasons"]


@pytest.fixture
def binding_inputs(tmp_path):
    config = TasksmithConfig(
        ledger_path=tmp_path / "ledger.json", ledger_limit_usd=100, campaign_id="fixture"
    )
    source = {
        "url": "https://github.com/example/library/pull/12",
        "repo": "example/library",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "base_tree_sha": "c" * 40,
        "head_tree_sha": "d" * 40,
    }
    patch = ArtifactRef(path="gold.patch", sha256="e" * 64, size_bytes=123)
    construction = {
        "source_receipt": {
            "source": dict(source),
            "patch_digest": patch.sha256,
            "captured_at": 1780000000.0,
        },
        "design": {
            "proposals": [
                {
                    "id": "batching",
                    "task_request": "Distribute whole batches while preserving order and incomplete tails.",
                    "included_behavior_ids": ["order", "tail"],
                    "verification_approach": "Compare exact round-robin sequences for multiple batch lengths.",
                    "independent_expectations": "Derive rank outputs independently with integer index arithmetic.",
                    "plausible_wrong_solutions": ["Drop the incomplete tail."],
                    "valid_alternatives": ["Use an explicit loop."],
                    "cost_and_uncertainty": "Small CPU fixtures.",
                }
            ],
            "selected_id": "batching",
            "selection_rationale": "One coherent behavior covers the complete public change.",
            "fewer_proposals_reason": "One logical task for this PR.",
        },
        "artifact": {
            "contract": {
                "title": "Distribute whole batches",
                "rationale": "Preserve ordering and tails across ranks.",
                "source_paths": ["src/example"],
                "requirements": [
                    {"id": "order", "behavior": "Preserve order", "tests": ["test_order"]},
                    {
                        "id": "tail",
                        "behavior": "Preserve tails",
                        "tests": ["test_tail", "test_empty"],
                    },
                ],
                "min_tests": 3,
                "mutations": [
                    {"name": "drop-tail", "rationale": "Incorrect tail", "script": "false"},
                    {"name": "reverse", "rationale": "Incorrect order", "script": "false"},
                ],
                "equivalents": [{"name": "loop", "rationale": "Valid loop", "script": "true"}],
            },
            "expected_values": {
                name: {
                    "provenance": "public_math",
                    "explanation": "Independent integer rank arithmetic.",
                    "evidence_ids": ["instruction"],
                }
                for name in ("test_order", "test_tail", "test_empty")
            },
            "cases": {
                name: ["Fixed rank fixture"] for name in ("test_order", "test_tail", "test_empty")
            },
            "public_evidence": {"order": ["instruction"], "tail": ["instruction"]},
            "source_origin_probe": "print('[1, 2]')",
            "reference_explanation": "The reference implements the public batch ordering and tail requirements.",
        },
    }
    return config, source, construction, patch


def test_bind_contracts_freezes_source_patch_and_collection(binding_inputs):
    config, source, construction, patch = binding_inputs
    original = copy.deepcopy(construction)
    result = bind_contracts(*binding_inputs)
    assert result.task.source.before_commit == source["base_sha"]
    assert result.task.source.reference_commit == result.oracle.source_commit == source["head_sha"]
    assert result.task.source.patch_digest == result.oracle.patch.sha256 == patch.sha256
    assert result.task.source.before_tree == source["base_tree_sha"]
    assert result.task.source.reference_tree == source["head_tree_sha"]
    assert result.task.collection == result.materialization.collection
    assert result.oracle.materialization_digest == digest(result.materialization)
    assert result.episode.horizon_seconds == config.trial_timeout_sec
    assert result.task.revision == 0 and result.task.parent_digest is None
    assert construction == original
    assert not config.ledger_path.exists()


def test_bind_contracts_rejects_reference_patch_substitution(binding_inputs):
    config, source, construction, patch = binding_inputs
    changed = patch.model_copy(update={"sha256": "f" * 64})
    with pytest.raises(ValueError, match="reference patch differs"):
        bind_contracts(config, source, construction, changed)


@pytest.mark.parametrize("key", ["repo", "base_sha", "head_sha", "base_tree_sha", "head_tree_sha"])
def test_bind_contracts_rejects_source_receipt_substitution(binding_inputs, key):
    config, source, construction, patch = binding_inputs
    construction["source_receipt"]["source"][key] = "other/repo" if key == "repo" else "f" * 40
    with pytest.raises(ValueError, match="Source receipt differs"):
        bind_contracts(config, source, construction, patch)


def test_bind_contracts_preserves_repair_lineage(binding_inputs):
    config, source, construction, patch = binding_inputs
    first = bind_contracts(*binding_inputs)
    parent = digest(first.task)
    construction.update(task_revision=1, parent_task_digest=parent)
    child = bind_contracts(config, source, construction, patch)
    assert child.task.revision == 1
    assert child.task.parent_digest == parent
    assert child.task.source == first.task.source
    assert child.oracle == first.oracle
    assert digest(child.task) != parent


@pytest.mark.parametrize("revision,parent", [(1, None), (0, "f" * 64), (2, "invalid")])
def test_bind_contracts_rejects_incoherent_repair_lineage(binding_inputs, revision, parent):
    config, source, construction, patch = binding_inputs
    construction.update(task_revision=revision, parent_task_digest=parent)
    with pytest.raises(ValueError):
        bind_contracts(config, source, construction, patch)
