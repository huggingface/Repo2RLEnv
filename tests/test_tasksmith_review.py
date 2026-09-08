from __future__ import annotations

import asyncio
import json
import time

import pytest

from repo2rlenv.curation.models import Contract
from repo2rlenv.tasksmith import review, worker
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.models import QUALITY_DIMENSIONS, PRIdentity, QualityReport

PR = PRIdentity(repository="huggingface/accelerate", number=3969)
REVISION = "a" * 64


@pytest.fixture
def config(tmp_path):
    return TasksmithConfig(
        ledger_path=tmp_path / "budget.json", ledger_limit_usd=100, campaign_id="test"
    )


@pytest.fixture
def contract():
    return Contract(
        title="Distribute dynamic batches",
        rationale="Keep whole-batch sharding semantics.",
        source_paths=["src/accelerate"],
        requirements=[
            {"id": "dynamic", "behavior": "Whole batches", "tests": ["test_dynamic"]},
            {"id": "tail", "behavior": "Complete tail", "tests": ["test_tail", "test_drop"]},
        ],
        min_tests=3,
        mutations=[
            {"name": "wrong-tail", "rationale": "Drop tail", "script": "false"},
            {"name": "wrong-order", "rationale": "Reorder", "script": "false"},
        ],
        equivalents=[{"name": "alternate", "rationale": "Alternate schedule", "script": "true"}],
    )


def arguments(config, tmp_path):
    return dict(
        config=config,
        budget=config.budget("candidate"),
        root=tmp_path / "review",
        deadline=time.time() + 300,
    )


def public_result(**kwargs):
    return {
        "status": "pass",
        "understood_outcome": "Distribute whole dynamic batches with explicit padding behavior.",
        **kwargs,
    }


def critic_result(**kwargs):
    return {
        "status": "pass",
        "explanation": "The described cases have independent expectations; the new mutation still needs remote execution.",
        "challenge": {
            "name": "distinct-tail",
            "rationale": "Replace complete tail padding with a dropped incomplete round.",
            "script": "python - <<'PY'\nfrom pathlib import Path\np=Path('src/accelerate/data_loader.py')\np.write_text(p.read_text().replace('yield batch', 'yield []'))\nPY\n",
        },
        "challenge_requirement_ids": ["tail"],
        "independent_expectations": [
            {
                "requirement_id": "tail",
                "input_case": "Three different batches are distributed across two logical ranks.",
                "expected_observation": "Rank one receives the second input batch and then the first batch as padding.",
                "provenance": {
                    "provenance": "independent_reference",
                    "explanation": "Round-robin division followed by one whole-batch padding step.",
                    "evidence_ids": ["instruction", "source_excerpts"],
                },
            }
        ],
        **kwargs,
    }


def final_result(**kwargs):
    return {
        "pr": PR.model_dump(),
        "revision_digest": REVISION,
        "criteria": {
            key: {
                "status": "pass",
                "score": 3,
                "explanation": "Supported by the supplied observed records.",
                "evidence_ids": ["instruction", "controls"],
            }
            for key in QUALITY_DIMENSIONS
        },
        **kwargs,
    }


def dossier():
    return {key: "Captured complete evidence for " + key for key in review.FINAL_REQUIRED_EVIDENCE}


async def read_all(call):
    inventory = json.loads(call["prompt"])["required_evidence"]
    for key, size in inventory.items():
        for start in range(0, size, 12000):
            response = await call["handlers"]["read_evidence"](
                requests=[{"evidence_id": key, "offset": start, "length": 12000}]
            )
            assert "pages" in json.loads(response)


def test_public_review_never_receives_private_evidence(config, tmp_path, monkeypatch):
    calls = []

    async def run(**call):
        calls.append(call)
        assert call["runtime"] == "langgraph"
        assert set(call["handlers"]) == {"submit_artifact", "revise_artifact"}
        assert "private-canary-value" not in call["prompt"]
        assert (
            await call["handlers"]["submit_artifact"](**public_result())
            == "Artifact committed. Do not call more tools; finish with a brief summary."
        )

    monkeypatch.setattr(worker, "run_agent", run)
    args = arguments(config, tmp_path)
    result = asyncio.run(
        review.comprehension_review(
            **args,
            instruction="Preserve dynamic batches and public padding behavior.",
            visible_files=["src/accelerate/data_loader.py"],
            reference_hashes=("private-canary-value",),
        )
    )
    assert result.status == "pass"
    assert calls[0]["max_cost"] == config.review_stage_limit_usd
    assert calls[0]["budget"] is args["budget"]
    assert calls[0]["max_turns"] == 16


def test_critic_partial_read_rejected_then_valid_artifact_committed(
    config, contract, tmp_path, monkeypatch
):
    async def run(**call):
        assert set(call["handlers"]) == {"submit_artifact", "revise_artifact", "read_evidence"}
        rejected = await call["handlers"]["submit_artifact"](**critic_result())
        assert "Read all required evidence" in rejected
        await read_all(call)
        bad = critic_result(challenge_requirement_ids=["invented"])
        assert "rejected" in await call["handlers"]["submit_artifact"](**bad)
        accepted = await call["handlers"]["submit_artifact"](**critic_result())
        assert "committed" in accepted

    monkeypatch.setattr(worker, "run_agent", run)
    result = asyncio.run(
        review.verifier_critic(
            **arguments(config, tmp_path),
            contract=contract,
            artifacts={
                "instruction": "Whole-batch sharding",
                "protected_tests": "Captured test source, never executed locally.",
                "source_excerpts": "Captured before/after public source.",
            },
        )
    )
    assert result.challenge_expected_reward == 0
    assert result.challenge.name == "distinct-tail"
    assert not (tmp_path / "src").exists()
    assert json.loads((tmp_path / "review/operation.json").read_text())["status"] == "completed"


def test_final_reads_all_and_cache_reuses_without_model(config, tmp_path, monkeypatch):
    calls = []

    async def run(**call):
        calls.append(call)
        assert "Read all required evidence" in await call["handlers"]["submit_artifact"](
            **final_result()
        )
        await read_all(call)
        assert "committed" in await call["handlers"]["submit_artifact"](**final_result())

    monkeypatch.setattr(worker, "run_agent", run)
    args = dict(**arguments(config, tmp_path), pr=PR, revision_digest=REVISION, evidence=dossier())
    first = asyncio.run(review.final_review(**args))
    assert isinstance(first, QualityReport)
    assert asyncio.run(review.final_review(**args)) == first
    assert len(calls) == 1
    operation = json.loads((tmp_path / "review/artifact.json").read_text())
    assert operation["input_digest"]
    # Corrupting a complete flag cannot replace interval evidence, even on cached output.
    receipt = tmp_path / "review/read-coverage.json"
    data = json.loads(receipt.read_text())
    data["spans"]["solver_1"] = []
    data["complete"] = True
    receipt.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="solver_1"):
        asyncio.run(review.final_review(**args))
    assert len(calls) == 1


def test_large_dossier_allows_reading_before_submission(config, tmp_path, monkeypatch):
    evidence = dossier()
    evidence["solver_0"] = "Captured solver action. " * 21000
    total = sum(map(len, evidence.values()))

    async def run(**call):
        minimum_reads = (total + review.MAX_READ_CHARACTERS - 1) // review.MAX_READ_CHARACTERS
        assert call["max_turns"] >= minimum_reads + 4
        assert call["max_cost"] == config.review_stage_limit_usd
        await read_all(call)
        assert "committed" in await call["handlers"]["submit_artifact"](**final_result())

    monkeypatch.setattr(worker, "run_agent", run)
    result = asyncio.run(
        review.final_review(
            **arguments(config, tmp_path), pr=PR, revision_digest=REVISION, evidence=evidence
        )
    )
    assert isinstance(result, QualityReport)


def test_completed_rejection_never_rerolled_and_changed_inputs_fail(config, tmp_path, monkeypatch):
    calls = []

    async def run(**call):
        calls.append(call)
        await call["handlers"]["submit_artifact"](
            **public_result(
                status="fail",
                required_repairs=[
                    {
                        "explanation": "The padding convention is missing from the public request.",
                        "evidence_ids": ["instruction"],
                    }
                ],
            )
        )

    monkeypatch.setattr(worker, "run_agent", run)
    args = dict(
        **arguments(config, tmp_path),
        instruction="Distribute batches.",
        visible_files=["source.py"],
    )
    assert asyncio.run(review.comprehension_review(**args)).status == "fail"
    assert asyncio.run(review.comprehension_review(**args)).status == "fail"
    with pytest.raises(ValueError, match="different inputs"):
        asyncio.run(
            review.comprehension_review(**{**args, "instruction": "Distribute whole batches."})
        )
    assert len(calls) == 1


def test_final_rejects_unknown_citation_and_na_within_same_bounded_stage(
    config, tmp_path, monkeypatch
):
    async def run(**call):
        await read_all(call)
        bad = final_result()
        bad["criteria"]["useful_scope"]["evidence_ids"] = ["invented"]
        assert "Unknown evidence IDs" in await call["handlers"]["submit_artifact"](**bad)
        bad = final_result()
        bad["criteria"]["oracle_validity"].update(status="not_applicable", score=None)
        assert "does not permit" in await call["handlers"]["submit_artifact"](**bad)
        bad = final_result(revision_digest="b" * 64)
        assert "does not match" in await call["handlers"]["submit_artifact"](**bad)
        bad = final_result(expert_review="reviewed")
        assert "cannot claim expert" in await call["handlers"]["submit_artifact"](**bad)
        await call["handlers"]["submit_artifact"](**final_result())

    monkeypatch.setattr(worker, "run_agent", run)
    result = asyncio.run(
        review.final_review(
            **arguments(config, tmp_path), pr=PR, revision_digest=REVISION, evidence=dossier()
        )
    )
    assert result.criteria["oracle_validity"].status == "pass"


@pytest.mark.parametrize(
    "missing", ["solver_1", "adversary", "submissions", "protected_tests", "source_witness"]
)
def test_missing_final_roles_fail_before_dispatch(config, tmp_path, monkeypatch, missing):
    async def forbidden(**kwargs):
        pytest.fail("Missing dossier must fail before model dispatch")

    monkeypatch.setattr(worker, "run_agent", forbidden)
    evidence = dossier()
    del evidence[missing]
    with pytest.raises(ValueError, match="missing required evidence"):
        asyncio.run(
            review.final_review(
                **arguments(config, tmp_path), pr=PR, revision_digest=REVISION, evidence=evidence
            )
        )
    assert not (tmp_path / "review").exists()


def test_unicode_partial_gaps_and_delivery_bound(tmp_path):
    reader = review._EvidenceReader({"a": "α" * 30000, "b": "β" * 30000}, tmp_path)
    result = json.loads(
        asyncio.run(
            reader.read(
                requests=[
                    {"evidence_id": "a", "offset": 12000, "length": 12000},
                    {"evidence_id": "b", "length": 12000},
                    {"evidence_id": "a", "length": 12000},
                ]
            )
        )
    )
    assert sum(len(page["text"]) for page in result["pages"]) == 24000
    assert "a: [0,12000)" in reader.missing()
    assert "a: [24000,30000)" in reader.missing()
    with pytest.raises(ValueError, match="Read all"):
        reader.require_complete()
    for key, start, length in [
        ("a", 0, 12000),
        ("a", 24000, 6000),
        ("b", 12000, 12000),
        ("b", 24000, 6000),
    ]:
        asyncio.run(reader.read(requests=[{"evidence_id": key, "offset": start, "length": length}]))
    reader.require_complete()
    review._EvidenceReader(reader.evidence, tmp_path).require_complete()


def test_invalid_batch_never_credits_undelivered_page(tmp_path):
    reader = review._EvidenceReader({"a": "abcdef"}, tmp_path)
    answer = asyncio.run(reader.read(requests=[{"evidence_id": "a"}, {"evidence_id": "missing"}]))
    assert answer.startswith("Invalid")
    assert reader.spans == {"a": []}
    assert not (tmp_path / "read-coverage.json").exists()


@pytest.mark.parametrize(
    "evidence", [{}, {"a": 123}, {"a": "x" * 512001}, {str(i): "x" for i in range(65)}]
)
def test_invalid_evidence_bound_fails_without_truncation(evidence):
    with pytest.raises(ValueError):
        review._snapshot(evidence)


def test_empty_supplementary_file_needs_no_fabricated_read(tmp_path):
    evidence = review._snapshot({"helper/__init__.py": "", "instruction": "Keep behavior."})
    reader = review._EvidenceReader(evidence, tmp_path)
    assert reader.missing() == ["instruction: [0,14)"]
    asyncio.run(reader.read(requests=[{"evidence_id": "instruction"}]))
    reader.require_complete()


def test_empty_mandatory_role_fails_before_model(config, tmp_path):
    evidence = dossier()
    evidence["solver_0"] = ""
    with pytest.raises(ValueError, match="roles must contain"):
        asyncio.run(
            review.final_review(
                **arguments(config, tmp_path), pr=PR, revision_digest=REVISION, evidence=evidence
            )
        )


def test_scanner_preserves_api_names_and_reports_exact_leads():
    text = (
        "Use target_parameters and state_dict.\nUse /private/gold.patch.\ngit show HEAD~1\n+++ b/file.py\n"
        + "b" * 40
    )
    flags = review.scan_instruction_leakage(text, ("b" * 40,))
    assert {row.kind for row in flags} == {
        "private_artifact",
        "history_access",
        "answer_patch",
        "reference_hash",
    }
    assert not any(row.line == 1 for row in flags)
    assert flags[0].line == 2 and flags[0].quote == "Use /private/gold.patch."


def test_unknown_cached_citation_is_revalidated(config, tmp_path, monkeypatch):
    async def run(**call):
        await call["handlers"]["submit_artifact"](**public_result())

    monkeypatch.setattr(worker, "run_agent", run)
    args = dict(
        **arguments(config, tmp_path),
        instruction="Keep public behavior.",
        visible_files=["src/example.py"],
    )
    asyncio.run(review.comprehension_review(**args))
    path = tmp_path / "review/artifact.json"
    stored = json.loads(path.read_text())
    stored["artifact"]["advisory"] = [
        {
            "explanation": "Additional explanation of the public behavior.",
            "evidence_ids": ["fabricated"],
        }
    ]
    path.write_text(json.dumps(stored))
    with pytest.raises(ValueError, match="Unknown evidence"):
        asyncio.run(review.comprehension_review(**args))


def test_budget_exception_remains_incomplete_no_retry(config, tmp_path, monkeypatch):
    from repo2rlenv.curation.budget import BudgetExceeded

    async def run(**call):
        raise BudgetExceeded("same shared scope exhausted")

    monkeypatch.setattr(worker, "run_agent", run)
    args = dict(
        **arguments(config, tmp_path),
        instruction="Keep public behavior.",
        visible_files=["src/example.py"],
    )
    with pytest.raises(BudgetExceeded):
        asyncio.run(review.comprehension_review(**args))
    assert json.loads((tmp_path / "review/operation.json").read_text())["status"] == "incomplete"
    with pytest.raises(RuntimeError, match="reconciliation"):
        asyncio.run(review.comprehension_review(**args))
