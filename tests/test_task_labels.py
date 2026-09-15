from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path

import pytest
import tomli_w

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, inspect_bundle, write_bundle
from repo2rlenv.emitter.evaluation import EvaluationLabel, generated_evaluation
from repo2rlenv.quality.labels import label_from_quality, read_evaluation, write_labeled_copy
from repo2rlenv.quality.loop.artifacts import probe_variant, task_identity
from repo2rlenv.quality.loop.models import (
    Assessment,
    Citation,
    LoopResult,
    Review,
    SemanticProbe,
    TrialRecord,
)
from repo2rlenv.task_labels import add_tasks_parser


@pytest.fixture
def task(tmp_path):
    return write_bundle(
        TaskBundle(
            name="fixture",
            org="tests",
            instruction="Restore the public behavior.",
            metadata={
                "recipe": "fixture",
                "recipe_version": "1",
                "reward_kinds": ["test_execution"],
            },
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 1\n", executable=True),
            },
        ),
        tmp_path / "original",
    )


@pytest.fixture
def quality(task, tmp_path):
    directory = tmp_path / "quality"
    assessment = Assessment(
        status="pass",
        score=4,
        explanation="Behavior is covered.",
        evidence=[Citation(path="instruction.md", quote="Restore the public behavior.")],
    )
    review = Review(
        summary="Usable task.",
        task=assessment,
        verifier=assessment,
        leakage=assessment,
        rollout="legitimate_failure",
        issues=[],
        probes=[],
        read_requests=[],
    )
    trials = []
    identity = task_identity(task)
    for index, (role, reward, kind) in enumerate(
        [
            ("baseline", 0.0, None),
            ("oracle", 1.0, None),
            ("probe", 0.0, "wrong_solution"),
            ("probe", 1.0, "valid_alternative"),
            ("rollout", 0.0, None),
        ]
    ):
        key = f"r0-{index}"
        probe, source = None, task
        if kind:
            probe = SemanticProbe(
                name=f"probe-{index}",
                kind=kind,
                rationale="Exercise behavior.",
                evidence=assessment.evidence,
                script="printf 'changed implementation'\n",
            )
            source = directory / "probes" / key / task.name
            probe_variant(task, probe, source)
        agent = {"baseline": "nop", "oracle": "oracle", "probe": "oracle", "rollout": "terminus-2"}[
            role
        ]
        trial_path = directory / "trials" / key / "job" / "trial" / "result.json"
        trial_path.parent.mkdir(parents=True)
        raw = {
            "config": {
                "agent": {"name": agent, "model_name": "test-model" if role == "rollout" else None},
                "task": {"path": str(source)},
            },
            "verifier_result": {"rewards": {"reward": reward}},
            "exception_info": None,
            "task_checksum": "a" * 64,
        }
        trial_path.write_text(json.dumps(raw))
        digest = hashlib.sha256(trial_path.read_bytes()).hexdigest()
        if kind:
            log = trial_path.parent / "agent/oracle.txt"
            log.parent.mkdir()
            log.write_text("__QUALITY_PROBE_COMPLETED__\n")
        (trial_path.parents[2] / "trial.json").write_text(
            json.dumps(
                {
                    "state": "completed",
                    "bundle_hash": task_identity(source),
                    "result_sha256": digest,
                }
            )
        )
        trials.append(
            TrialRecord(
                role=role,
                bundle_hash=task_identity(source),
                result=str(trial_path),
                result_sha256=digest,
                agent=agent,
                model="test-model" if role == "rollout" else None,
                reward=reward,
                exception_type=None,
                binding="receipt",
                probe=probe,
                probe_installed=True if kind else None,
            )
        )
    result = LoopResult(
        status="usable",
        source_hash=identity,
        bundle_hash=identity,
        task_path=str(task),
        repairs=0,
        review=review,
        trials=trials,
        reasons=[],
        accounted_usd="0",
        reserved_usd="0",
    )
    path = directory / "result.json"
    path.write_text(result.model_dump_json(indent=2))
    return path


def update_quality(path, change):
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def test_default_is_deterministic_unverified_with_unknown_provenance():
    first, second = generated_evaluation(), generated_evaluation()
    assert first == second
    assert first["status"] == "unverified" and first["provenance"] == "unknown"
    assert "checked_at" not in first and "subject_bundle_hash" not in first


def test_missing_label_never_implies_verification(task):
    assert read_evaluation(task).status == "unverified"


def test_copy_preserves_original_and_bundle_identity(task, tmp_path):
    original = (task / "task.toml").read_bytes()
    target = write_labeled_copy(
        task,
        tmp_path / "labeled",
        EvaluationLabel(
            status="blocked",
            stage="bootstrap",
            reason_codes=["runtime_incompatible"],
            detail="Image lacks the required interpreter; rebuild and rerun controls.",
        ),
    )
    assert (task / "task.toml").read_bytes() == original
    assert (target / "tests/test.sh").read_bytes() == (task / "tests/test.sh").read_bytes()
    assert inspect_bundle(target)["integrity_passed"]
    assert task_identity(target) == task_identity(task)
    label = read_evaluation(target)
    assert label.status == "blocked"
    assert label.source_task_toml_sha256 == hashlib.sha256(original).hexdigest()
    assert label.provenance == "unknown"
    with pytest.raises(FileExistsError):
        write_labeled_copy(task, target, label)


def test_real_control_pattern_can_label_legitimate_solver_failure(task, quality, tmp_path):
    label = label_from_quality(task, quality, provenance="assisted")
    assert label.status == "verified" and label.provenance == "assisted"
    assert len(label.evidence) == 6
    target = write_labeled_copy(task, tmp_path / "accepted", label)
    assert read_evaluation(target).status == "verified"
    assert task_identity(target) == task_identity(task)


@pytest.mark.parametrize(
    "change", ["missing", "tampered", "receipt", "probe_parent", "probe_marker", "outcome"]
)
def test_verification_rejects_incomplete_or_misbound_evidence(task, quality, change):
    result = json.loads(quality.read_text())
    trial = result["trials"][2]
    raw = Path(trial["result"])
    if change == "missing":
        raw.unlink()
    elif change == "tampered":
        raw.write_text(raw.read_text() + " ")
    elif change == "receipt":
        receipt = raw.parents[2] / "trial.json"
        receipt.write_text(receipt.read_text().replace(trial["bundle_hash"], "sha256:" + "b" * 64))
    elif change == "probe_parent":
        source = Path(json.loads(raw.read_text())["config"]["task"]["path"])
        manifest = source.parent / "probe.json"
        manifest.write_text(
            manifest.read_text().replace(result["bundle_hash"], "sha256:" + "b" * 64)
        )
    elif change == "probe_marker":
        (raw.parent / "agent/oracle.txt").write_text("Execution failed before mutation.")
    else:
        result["trials"][2]["reward"] = 0.5
        quality.write_text(json.dumps(result))
    with pytest.raises(ValueError):
        label_from_quality(task, quality)


def test_worker_sandbox_probe_paths_resolve_preserved_controller_copy(task, quality):
    result = json.loads(quality.read_text())
    for trial in result["trials"]:
        if trial["role"] != "probe":
            continue
        raw = Path(trial["result"])
        value = json.loads(raw.read_text())
        value["config"]["task"]["path"] = f"/nonexistent-sandbox/{task.name}"
        raw.write_text(json.dumps(value))
        trial["result_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        receipt = raw.parents[2] / "trial.json"
        record = json.loads(receipt.read_text())
        record["result_sha256"] = trial["result_sha256"]
        receipt.write_text(json.dumps(record))
    quality.write_text(json.dumps(result))
    assert label_from_quality(task, quality).status == "verified"


@pytest.mark.parametrize("valid_checksum", [True, False])
def test_imported_worker_checksum_requires_exact_preserved_local_task(
    task, quality, valid_checksum
):
    pytest.importorskip("harbor")
    from repo2rlenv.quality.loop.artifacts import parse_task

    result = json.loads(quality.read_text())
    trial = result["trials"][0]
    trial["binding"] = "harbor_checksum"
    raw = Path(trial["result"])
    value = json.loads(raw.read_text())
    value["config"]["task"]["path"] = f"/nonexistent-sandbox/{task.name}"
    value["task_checksum"] = parse_task(task).checksum if valid_checksum else "f" * 64
    raw.write_text(json.dumps(value))
    trial["result_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    quality.write_text(json.dumps(result))
    if valid_checksum:
        assert label_from_quality(task, quality).status == "verified"
    else:
        with pytest.raises(ValueError, match="checksum"):
            label_from_quality(task, quality)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("needs_evidence", "unverified"),
        ("reviewed", "unverified"),
        ("needs_repair", "needs_repair"),
        ("budget_exhausted", "blocked"),
    ],
)
def test_unaccepted_attempts_are_retained_with_missing_evidence(
    task, quality, tmp_path, status, expected
):
    result = json.loads(quality.read_text())
    result.update(status=status, reasons=["Retain this attempted verifier for diagnosis."])
    Path(result["trials"][0]["result"]).unlink()
    quality.write_text(json.dumps(result))
    label = label_from_quality(task, quality)
    assert label.status == expected and "evidence_unavailable" in label.reason_codes
    target = write_labeled_copy(task, tmp_path / "retained", label)
    assert read_evaluation(target).status == expected


def test_label_cannot_rebind_a_changed_task(task, quality):
    config = tomllib.loads((task / "task.toml").read_text())
    config["agent"]["timeout_sec"] += 1
    (task / "task.toml").write_text(tomli_w.dumps(config))
    with pytest.raises(ValueError, match="differs"):
        label_from_quality(task, quality)


def test_copy_checks_quality_again_before_publishing(task, quality, tmp_path):
    label = label_from_quality(task, quality)
    quality.write_text(quality.read_text() + " ")
    with pytest.raises(ValueError, match="unchanged"):
        write_labeled_copy(task, tmp_path / "forged", label)
    assert not (tmp_path / "forged").exists()


def test_source_links_and_nested_destinations_rejected(task, tmp_path):
    with pytest.raises(ValueError, match="outside"):
        write_labeled_copy(task, task / "copy", EvaluationLabel())
    (task / "tests/link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        write_labeled_copy(task, tmp_path / "linked", EvaluationLabel())


@pytest.mark.parametrize(
    "values",
    [
        {"status": "verified"},
        {"reason_codes": []},
        {"reason_codes": ["Some reason"]},
        {"reason_codes": ["same", "same"]},
        {"checked_at": "2026-09-13T10:00:00"},
    ],
)
def test_schema_rejects_unbound_claims_and_nonuniform_codes(values):
    with pytest.raises(ValueError):
        EvaluationLabel(**values)


def test_cli_keeps_verification_behind_evidence_import(task, quality, tmp_path):
    parser = argparse.ArgumentParser()
    add_tasks_parser(parser.add_subparsers())
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "tasks",
                "label",
                str(task),
                "--out",
                str(tmp_path / "invalid"),
                "--status",
                "verified",
            ]
        )
    args = parser.parse_args(
        [
            "tasks",
            "label",
            str(task),
            "--out",
            str(tmp_path / "cli-copy"),
            "--quality-result",
            str(quality),
            "--json",
        ]
    )
    assert args.func(args) == 0
    assert read_evaluation(tmp_path / "cli-copy").status == "verified"


def test_label_from_another_bundle_is_rejected(task, quality):
    update_quality(quality, lambda value: value.update(bundle_hash="sha256:" + "c" * 64))
    with pytest.raises(ValueError, match="different"):
        label_from_quality(task, quality)
