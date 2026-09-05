from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.campaign import ADMISSION_VERSION
from repo2rlenv.curation.inference import inference_digest
from repo2rlenv.curation.models import (
    CRITERIA,
    CampaignConfig,
    Review,
    TrialEvidence,
    review_scores,
)
from repo2rlenv.curation.publish import evidence_snapshot, publish_evidence
from repo2rlenv.curation.review_evidence import ReviewEvidenceError

review_module = importlib.import_module("repo2rlenv.curation.review")


def write(path: Path, value: dict | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)


@pytest.fixture
def api(monkeypatch):
    import huggingface_hub

    class FakeApi:
        private = True

        def __init__(self):
            self.created = []
            self.uploads = []

        def create_bucket(self, bucket, *, private, exist_ok):
            self.created.append((bucket, private, exist_ok))

        def bucket_info(self, bucket):
            return SimpleNamespace(private=self.private)

        def batch_bucket_files(self, bucket, *, add):
            for data, name in add:
                self.uploads.append(
                    (name, data if isinstance(data, bytes) else Path(data).read_bytes())
                )

    fake = FakeApi()
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: fake)
    return fake


def successful_review(config):
    return Review(
        criteria={
            name: {
                "score": 0
                if config.acceptance_policy == "validity" and name == "intrinsic_difficulty"
                else 4,
                "outcome": "fail"
                if config.acceptance_policy == "validity" and name == "intrinsic_difficulty"
                else "pass",
                "explanation": "Retained deterministic reviewer fixture supports this criterion.",
                "evidence": ["task/instruction.md"],
            }
            for name in CRITERIA
        },
        blockers=[],
        failure_attribution={
            f"solver-{index}-{attempt}": "solved"
            for index in range(len(config.solver_models))
            for attempt in range(config.solver_attempts)
        },
        reward_hacks=[],
        suggested_repairs=[],
        adversary_assessment="attempted_hack",
    )


def create_review_receipt(folder, task, config, trial_damage=None):
    """Run the actual receipt writer, substituting only the model's read actions."""
    result = successful_review(config)
    trials = []
    rewards = {
        "baseline": 0,
        "tamper": 0,
        "pytest-tamper": 0,
        "mutation-empty": 0,
        "mutation-nested": 0,
        "equivalent-alternative": 1,
        **{f"oracle-{index}": 1 for index in range(config.oracle_repeats)},
        **{label: 1 for label in result.failure_attribution},
        "adversary": 0,
    }
    if trial_damage == "missing_oracle":
        del rewards["oracle-0"]
    elif trial_damage == "passing_mutation":
        rewards["mutation-empty"] = 1
    for label, reward in rewards.items():
        trial_path = folder / "trials" / (label + "-id")
        write(
            trial_path / "artifacts/manifest.json",
            json.dumps([{"source": "/workspace/src/pkg", "status": "ok"}]),
        )
        write(
            trial_path / "artifacts/workspace/src/pkg/source.py",
            "value = 1\n" if label.startswith("solver") else "value = 0\n",
        )
        write(
            trial_path / "agent/trace.jsonl",
            json.dumps(
                {
                    "kind": "model",
                    "message": {"role": "assistant", "content": f"Completed {label}."},
                }
            )
            + "\n",
        )
        model = (
            config.solver_models[int(label.split("-")[1])]
            if label.startswith("solver-")
            else (config.author_model if label == "adversary" else None)
        )
        trials.append(
            TrialEvidence(
                label=label,
                task_digest=digest_task(task),
                path=str(trial_path),
                reward=reward,
                model=model,
                inference_digest=inference_digest(model, adversary=label == "adversary")
                if model
                else None,
            )
        )

    async def judge(**kwargs):
        texts = json.loads((folder / "review-projections.json").read_text())["texts"]
        texts.update(
            {
                name: (folder / name).read_text()
                for name in json.loads((folder / "review-coverage.json").read_text())[
                    "required_sha256"
                ]
                if name not in texts
            }
        )
        calls, outputs = [], []
        for name, text in texts.items():
            assert 0 < len(text) <= 16000
            identity = f"read-{len(calls)}"
            arguments = {"path": name, "limit": 16000}
            calls.append(
                {
                    "id": identity,
                    "function": {"name": "read_evidence", "arguments": json.dumps(arguments)},
                }
            )
            outputs.append(
                {
                    "role": "tool",
                    "tool_call_id": identity,
                    "content": await kwargs["handlers"]["read_evidence"](**arguments),
                }
            )
        assert kwargs["validate_final"](result.model_dump_json()) is None
        return {
            "messages": [
                {"role": "system", "content": kwargs["system"]},
                {"role": "user", "content": kwargs["prompt"]},
                {"role": "assistant", "tool_calls": calls},
                *outputs,
                {"role": "assistant", "content": result.model_dump_json()},
            ],
            "turns": 2,
            "cost": 0,
        }

    with (
        patch.object(review_module, "run_agent", judge),
        patch.object(
            review_module, "completion", side_effect=AssertionError("Unexpected paid call")
        ),
        patch.object(Budget, "reserve", side_effect=AssertionError("Unexpected reservation")),
    ):
        asyncio.run(
            review_module.review(
                task,
                folder,
                trials,
                model=config.judge_model,
                budget=Budget(folder / "unused-budget.json", 1),
                acceptance_policy=config.acceptance_policy,
            )
        )
    write(
        folder / "evidence.json",
        {
            "admission_version": ADMISSION_VERSION,
            "task_digest": digest_task(task),
            "trials": [trial.model_dump() for trial in trials],
        },
    )
    return result


def populate(root: Path, *, comparison: bool, config=None, trial_damage=None):
    config = config or CampaignConfig()
    rows = []
    for runtime in ["langgraph", "pi", "opencode"] if comparison else [None]:
        task = root / "tasks"
        if runtime:
            task /= runtime
        task /= "example-project-1"
        write(
            task / "contract.json",
            {
                "title": "Widget inputs",
                "rationale": "Fixture for admission publication.",
                "source_paths": ["src/pkg"],
                "requirements": [
                    {"id": name, "behavior": f"Handle {name} inputs", "tests": [f"test_{name}"]}
                    for name in ("empty", "nested")
                ],
                "mutations": [
                    {"name": name, "rationale": f"Miss {name} inputs", "script": "true"}
                    for name in ("empty", "nested")
                ],
                "equivalents": [
                    {"name": "alternative", "rationale": "A valid alternative", "script": "true"}
                ],
                "min_tests": 3,
            },
        )
        write(task / "instruction.md", "Return the documented value from src/pkg.\n")
        write(task / "solution/patch.diff", "--- a/source.py\n+++ b/source.py\n")
        folder = root / "candidates" / (runtime or "campaign") / "example/run/revision-0"
        shutil.copytree(task, folder / "task")
        result = create_review_receipt(folder, folder / "task", config, trial_damage)
        row = {
            "id": "example-project-1",
            "status": "accepted",
            "task_digest": digest_task(task),
            "admission_version": ADMISSION_VERSION,
            "review_path": str((folder / "review.json").relative_to(root)),
            "reasons": [],
            "execution_errors": [],
            **review_scores(result, config),
        }
        if runtime:
            row["runtime"] = runtime
        rows.append(row)
    name = "comparison.json" if comparison else "manifest.json"
    manifest = {"rows" if comparison else "accepted": rows, "human_review": "pending"}
    write(root / name, manifest)
    write(root / "config.json", config.model_dump())
    return name, manifest


@pytest.mark.parametrize("comparison", [False, True])
def test_private_publication_supports_campaign_and_comparison_manifests(tmp_path, api, comparison):
    name, _ = populate(tmp_path, comparison=comparison)
    evidence = tmp_path / "candidates/run/revision-0"
    text = "actual_changed_source = 1\n"
    key = "trials/solver-0-0/artifacts/workspace/src/pkg.py"
    write(
        evidence / "review-submissions.json",
        {
            "schema_version": 1,
            "texts": {key: text},
            "sha256": {key: hashlib.sha256(text.encode()).hexdigest()},
        },
    )
    write(
        evidence / "review-evidence.json", {"submission_text_snapshot": "review-submissions.json"}
    )
    write(evidence / "trials/solver-0-0/artifacts/workspace/src/pkg.py", "raw export excluded")
    url = publish_evidence(tmp_path, "owner/private-evidence")
    assert api.created == [("owner/private-evidence", True, True)]
    assert url.startswith("https://huggingface.co/buckets/owner/private-evidence/tree/")
    uploaded = dict(api.uploads)
    prefix = url.rsplit("/", 1)[1]
    assert f"{prefix}/{name}" in uploaded
    snapshot = json.loads(uploaded[f"{prefix}/candidates/run/revision-0/review-submissions.json"])
    assert snapshot["texts"][key] == text
    assert not any("/artifacts/" in path for path in uploaded)
    checksums = json.loads(uploaded[f"{prefix}/checksums.json"])
    for path, checksum in checksums.items():
        assert hashlib.sha256(uploaded[f"{prefix}/{path}"]).hexdigest() == checksum
    for runtime in ["langgraph", "pi", "opencode"] if comparison else [None]:
        relative = f"tasks/{runtime}/example-project-1" if runtime else "tasks/example-project-1"
        assert f"{prefix}/{relative}/solution/patch.diff" in uploaded


@pytest.mark.parametrize("comparison", [False, True])
@pytest.mark.parametrize("damage", ["digest", "missing", "admission"])
def test_publication_refuses_changed_missing_or_stale_accepted_tasks(
    tmp_path, api, comparison, damage
):
    name, manifest = populate(tmp_path, comparison=comparison)
    row = manifest["rows" if comparison else "accepted"][0]
    task = tmp_path / "tasks"
    if comparison:
        task /= row["runtime"]
    task /= row["id"]
    if damage == "digest":
        (task / "contract.json").write_text("changed")
    elif damage == "missing":
        shutil.rmtree(task)
    else:
        row["admission_version"] -= 1
        write(tmp_path / name, manifest)
    with pytest.raises(ValueError, match="Accepted task"):
        publish_evidence(tmp_path, "owner/private-evidence")
    assert not api.created
    assert not api.uploads


def test_public_existing_bucket_never_receives_evidence(tmp_path, api):
    populate(tmp_path, comparison=True)
    api.private = False
    with pytest.raises(ValueError, match="private bucket"):
        publish_evidence(tmp_path, "owner/public-bucket")
    assert not api.uploads


def test_snapshot_prunes_runtime_credentials_and_caches_but_preserves_traces(tmp_path):
    write(tmp_path / "comparison.json", {"rows": []})
    runtime = tmp_path / "candidates/opencode/example/run/author-0-runtime"
    for name in (
        "opencode-events.jsonl",
        "opencode-messages.json",
        "runtime-result.json",
        "runtime-stderr.log",
        "native-session.jsonl",
    ):
        write(runtime / name, "retained evidence")
    write(runtime / "runner-config.json", "local bridge credential")
    write(runtime / "auth.json", "credential")
    write(runtime / "opencode-session/server.log", "server evidence")
    write(runtime / "opencode-session/bridge.json", "local bridge credential")
    write(runtime / "opencode-session/config/opencode/opencode.json", "provider configuration")
    write(runtime / "home/.config/settings.json", "ambient settings")
    linked = runtime / "opencode-session/config/opencode/node_modules"
    linked.symlink_to(tmp_path.parent, target_is_directory=True)
    write(tmp_path / "review-submissions.json", {"texts": {"source": "inspected source"}})
    with evidence_snapshot(tmp_path) as snapshot:
        copied = snapshot / runtime.relative_to(tmp_path)
        assert (copied / "opencode-events.jsonl").read_text() == "retained evidence"
        assert (copied / "native-session.jsonl").exists()
        assert (copied / "runtime-stderr.log").exists()
        assert (copied / "opencode-session/server.log").read_text() == "server evidence"
        assert not (copied / "runner-config.json").exists()
        assert not (copied / "auth.json").exists()
        assert not (copied / "opencode-session/bridge.json").exists()
        assert not (copied / "opencode-session/config").exists()
        assert not (copied / "home").exists()
        write(tmp_path / "review-submissions.json", {"texts": {"source": "changed later"}})
        assert (
            json.loads((snapshot / "review-submissions.json").read_text())["texts"]["source"]
            == "inspected source"
        )


def test_comparison_rejects_invalid_runtime_and_ambiguous_manifests(tmp_path, api):
    name, manifest = populate(tmp_path, comparison=True)
    manifest["rows"][0]["runtime"] = "../outside"
    write(tmp_path / name, manifest)
    with pytest.raises(ValueError, match="comparison runtime"):
        publish_evidence(tmp_path, "owner/private-evidence")
    write(tmp_path / "manifest.json", {"accepted": []})
    with pytest.raises(ValueError, match="exactly one"):
        publish_evidence(tmp_path, "owner/private-evidence")
    assert not api.created


def populate_pilot(root: Path, *, accepted=True, absolute_review=True):
    config = CampaignConfig(
        acceptance_policy="validity",
        submission_policy="conversion",
        max_candidate_drafts=3,
        require_verification_plan=True,
        specification_review=True,
        verifier_review=True,
    )
    manifest = {"rows": [], "human_review": "pending"}
    if accepted:
        _, prior = populate(root, comparison=False, config=config)
        row = prior["accepted"][0]
        if absolute_review:
            row["review_path"] = str(root / row["review_path"])
        manifest["rows"].append(row)
    write(root / "manifest.json", manifest)
    write(root / "config.json", config.model_dump())
    write(root / "protocol.json", {"id": "test-pilot", "config": config.model_dump()})
    return manifest


def test_private_publication_supports_pilot_with_no_admissions(tmp_path, api):
    manifest = populate_pilot(tmp_path, accepted=False)
    manifest["rows"].append({"id": "failed-example", "status": "repair_limit"})
    write(tmp_path / "manifest.json", manifest)

    url = publish_evidence(tmp_path, "owner/private-evidence")

    prefix = url.rsplit("/", 1)[1]
    uploaded = dict(api.uploads)
    assert json.loads(uploaded[f"{prefix}/manifest.json"])["rows"] == manifest["rows"]
    assert f"{prefix}/protocol.json" in uploaded
    assert f"{prefix}/config.json" in uploaded
    assert api.created == [("owner/private-evidence", True, True)]


@pytest.mark.parametrize("absolute_review", [False, True])
def test_pilot_validity_admission_keeps_campaign_paths_and_score_receipt(
    tmp_path, api, absolute_review
):
    manifest = populate_pilot(tmp_path, absolute_review=absolute_review)

    url = publish_evidence(tmp_path, "owner/private-evidence")

    prefix = url.rsplit("/", 1)[1]
    uploaded = dict(api.uploads)
    assert f"{prefix}/tasks/example-project-1/contract.json" in uploaded
    assert f"{prefix}/candidates/campaign/example/run/revision-0/review.json" in uploaded
    row = json.loads(uploaded[f"{prefix}/manifest.json"])["rows"][0]
    assert row == manifest["rows"][0]
    assert row["score"] == row["validity_score"] == 100
    assert row["legacy_score"] == 90
    assert row["intrinsic_difficulty_score"] == 0


@pytest.mark.parametrize(
    "damage",
    ["digest", "missing_task", "version", "score_receipt", "missing_review", "row_policy"],
)
def test_pilot_publication_preserves_admission_checks(tmp_path, api, damage):
    manifest = populate_pilot(tmp_path)
    row = manifest["rows"][0]
    if damage == "digest":
        write(tmp_path / "tasks/example-project-1/contract.json", "changed")
    elif damage == "missing_task":
        shutil.rmtree(tmp_path / "tasks/example-project-1")
    elif damage == "version":
        row["admission_version"] -= 1
    elif damage == "score_receipt":
        row["validity_score"] = 99
    elif damage == "missing_review":
        Path(row["review_path"]).unlink()
    elif damage == "row_policy":
        row["acceptance_policy"] = "legacy"
    write(tmp_path / "manifest.json", manifest)

    with pytest.raises(ValueError, match="Accepted"):
        publish_evidence(tmp_path, "owner/private-evidence")

    assert not api.created
    assert not api.uploads


@pytest.mark.parametrize(
    ("kind", "policy"),
    [
        ("campaign", "legacy"),
        ("campaign", "validity"),
        ("comparison", "legacy"),
        ("comparison", "validity"),
        ("pilot", "validity"),
    ],
)
@pytest.mark.parametrize("damage", ["missing", "unread", "changed_review"])
def test_publication_requires_complete_bound_review_receipt(tmp_path, api, kind, policy, damage):
    if kind == "pilot":
        row = populate_pilot(tmp_path)["rows"][0]
    else:
        _, manifest = populate(
            tmp_path,
            comparison=kind == "comparison",
            config=CampaignConfig(acceptance_policy=policy),
        )
        row = manifest["rows" if kind == "comparison" else "accepted"][0]
    folder = (tmp_path / row["review_path"]).parent
    coverage = folder / "review-coverage.json"
    if damage == "missing":
        coverage.unlink()
    elif damage == "unread":
        receipt = json.loads(coverage.read_text())
        receipt["reads"]["review-actions/solver-1-0.txt"] = []
        write(coverage, receipt)
    else:
        result = Review.model_validate_json((folder / "review.json").read_text())
        result.criteria["realism"].explanation = "Edited after the reviewer finished."
        write(folder / "review.json", result.model_dump())

    with pytest.raises(ReviewEvidenceError):
        publish_evidence(tmp_path, "owner/private-evidence")

    assert not api.created
    assert not api.uploads


@pytest.mark.parametrize("policy", ["legacy", "validity"])
@pytest.mark.parametrize("damage", ["missing_oracle", "passing_mutation"])
def test_valid_review_receipt_cannot_override_failed_execution_gates(tmp_path, api, policy, damage):
    config = CampaignConfig(acceptance_policy=policy)
    _, manifest = populate(tmp_path, comparison=False, config=config, trial_damage=damage)
    row = manifest["accepted"][0]
    folder = (tmp_path / row["review_path"]).parent
    trials = [
        TrialEvidence.model_validate(item)
        for item in json.loads((folder / "evidence.json").read_text())["trials"]
    ]
    assert review_module.validate_review_receipt(
        folder,
        folder / "task",
        trials,
        model=config.judge_model,
        acceptance_policy=policy,
    ) == successful_review(config)

    failed_label = "oracle-0" if damage == "missing_oracle" else "mutation-empty"
    with pytest.raises(ValueError, match=f"Published evidence fails admission: {failed_label}"):
        publish_evidence(tmp_path, "owner/private-evidence")

    assert not api.created
    assert not api.uploads


@pytest.mark.parametrize("field", ["acceptance_policy", "submission_policy", "acceptance_score"])
def test_pilot_publication_rejects_protocol_configuration_drift(tmp_path, api, field):
    populate_pilot(tmp_path, accepted=False)
    protocol = json.loads((tmp_path / "protocol.json").read_text())
    protocol["config"][field] = 80 if field == "acceptance_score" else "legacy"
    write(tmp_path / "protocol.json", protocol)

    with pytest.raises(ValueError, match="Pilot protocol configuration mismatch"):
        publish_evidence(tmp_path, "owner/private-evidence")

    assert not api.created


@pytest.mark.parametrize(
    "damage",
    [
        "missing_protocol",
        "missing_config",
        "both_row_keys",
        "campaign_with_protocol",
        "comparison_with_protocol",
        "protocol_without_config",
        "manifest_config_drift",
        "nonlist_rows",
        "row_without_status",
    ],
)
def test_pilot_publication_rejects_ambiguous_or_incomplete_metadata(tmp_path, api, damage):
    manifest = populate_pilot(tmp_path, accepted=False)
    if damage == "missing_protocol":
        (tmp_path / "protocol.json").unlink()
    elif damage == "missing_config":
        (tmp_path / "config.json").unlink()
    elif damage == "both_row_keys":
        manifest["accepted"] = []
    elif damage == "campaign_with_protocol":
        manifest = {"accepted": []}
    elif damage == "comparison_with_protocol":
        (tmp_path / "manifest.json").rename(tmp_path / "comparison.json")
    elif damage == "protocol_without_config":
        write(tmp_path / "protocol.json", {"id": "test-pilot"})
    elif damage == "manifest_config_drift":
        config = json.loads((tmp_path / "config.json").read_text())
        config["submission_policy"] = "legacy"
        manifest["config"] = config
    elif damage == "nonlist_rows":
        manifest["rows"] = {}
    elif damage == "row_without_status":
        manifest["rows"] = [{"id": "unknown"}]
    if damage != "comparison_with_protocol":
        write(tmp_path / "manifest.json", manifest)

    with pytest.raises(ValueError):
        publish_evidence(tmp_path, "owner/private-evidence")

    assert not api.created
    assert not api.uploads
