from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest

from repo2rlenv.curation.budget import BudgetExceeded
from repo2rlenv.tasksmith import inline_review as module
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.evidence_projection import project_dossier
from repo2rlenv.tasksmith.models import QUALITY_DIMENSIONS, PRIdentity


@pytest.fixture
def inputs(tmp_path):
    config = TasksmithConfig(
        ledger_path=tmp_path / "budget.json", ledger_limit_usd=100, campaign_id="inline"
    )
    return {
        "config": config,
        "budget": config.budget("candidate"),
        "root": tmp_path / "review",
        "deadline": time.time() + 300,
        "pr": PRIdentity(repository="huggingface/accelerate", number=3850),
        "revision_digest": "a" * 64,
        "evidence": {key: "Captured evidence: " + key for key in module.FINAL_REQUIRED_EVIDENCE},
        "prior_review_charge_usd": 0.82148175,
    }


def response(inputs, *, finish="tool_calls", change=None):
    report = {
        "pr": inputs["pr"].model_dump(),
        "revision_digest": inputs["revision_digest"],
        "criteria": {
            key: {
                "status": "pass",
                "score": 3,
                "explanation": "Supported by concrete source and execution evidence.",
                "evidence_ids": ["instruction", "controls"],
            }
            for key in QUALITY_DIMENSIONS
        },
    }
    if change:
        change(report)
    raw = {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "emit_quality_report",
                                "arguments": json.dumps(report),
                            }
                        }
                    ]
                },
            }
        ],
    }
    return SimpleNamespace(model_dump=lambda **kwargs: raw)


def transport(monkeypatch, result, inspect=lambda value: None):
    calls = []

    async def acompletion(**kwargs):
        inspect(kwargs)
        calls.append(kwargs)
        return result

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            get_model_info=lambda model: {
                "input_cost_per_token": 0.000005,
                "output_cost_per_token": 0.000025,
            },
            acompletion=acompletion,
            completion_cost=lambda **kwargs: 0.11,
        ),
    )
    return calls


def delivered_evidence(prompt):
    cursor = prompt.index("\nEVIDENCE ") + 1
    result = {}
    while cursor < len(prompt):
        assert prompt.startswith("EVIDENCE ", cursor)
        end = prompt.index("\n", cursor)
        header = json.loads(prompt[cursor + len("EVIDENCE ") : end])
        start = end + 1
        cursor = start + header["characters"]
        result[header["id"]] = prompt[start:cursor]
        assert prompt[cursor] == "\n"
        cursor += 1
    return result


def test_full_initial_prompt_survives_real_budget_transport_and_completed_recovery(
    inputs, monkeypatch
):
    inputs["evidence"]["solver_0"] = 'Actual tool output: "\\\x00🦊\n' * 2000
    original = dict(inputs["evidence"])

    def inspect(call):
        assert delivered_evidence(call["messages"][1]["content"]) == original
        assert call["max_tokens"] == 3500
        assert call["tool_choice"] == "required"

    calls = transport(monkeypatch, response(inputs), inspect)
    result = asyncio.run(module.final_review_inline(**inputs))
    assert asyncio.run(module.final_review_inline(**inputs)) == result
    assert len(calls) == 1
    assert inputs["budget"].spent == pytest.approx(0.11)
    op = json.loads((inputs["root"] / "operation.json").read_text())
    assert op["prior_review_charge_usd"] == 0.82148175
    assert op["charged_or_reserved_usd"] == pytest.approx(0.11)
    delivery = json.loads((inputs["root"] / "delivery.json").read_text())
    assert set(delivery["evidence_sha256"]) == set(original)
    (inputs["root"] / "response.json").write_text("changed")
    with pytest.raises(ValueError, match="evidence changed"):
        asyncio.run(module.final_review_inline(**inputs))


def test_reservation_includes_prior_review_cost_and_never_dispatches_over_limit(
    inputs, monkeypatch
):
    # This fits a fresh $3 allocation but not the remaining original $2.1785.
    inputs["evidence"]["solver_0"] = "x" * 330_000
    calls = transport(monkeypatch, response(inputs))
    with pytest.raises(BudgetExceeded):
        asyncio.run(module.final_review_inline(**inputs))
    assert not calls and inputs["budget"].spent == 0
    with pytest.raises(ValueError, match="no reroll"):
        asyncio.run(module.final_review_inline(**inputs))


@pytest.mark.parametrize(
    "finish,change",
    [
        ("length", None),
        ("tool_calls", lambda report: report.update(revision_digest="b" * 64)),
        ("tool_calls", lambda report: report.update(expert_review="approved")),
        (
            "tool_calls",
            lambda report: report["criteria"][next(iter(QUALITY_DIMENSIONS))].update(
                evidence_ids=["invented"]
            ),
        ),
    ],
)
def test_incomplete_or_invalid_model_result_cannot_be_retried(inputs, monkeypatch, finish, change):
    calls = transport(monkeypatch, response(inputs, finish=finish, change=change))
    with pytest.raises(ValueError):
        asyncio.run(module.final_review_inline(**inputs))
    with pytest.raises(ValueError, match="no reroll"):
        asyncio.run(module.final_review_inline(**inputs))
    assert len(calls) == 1


def test_projected_evidence_preserves_previous_judge_counterevidence(inputs, monkeypatch):
    inputs["evidence"]["prior_review"] = (
        "The previous judge identified this possible verifier gap. " * 20
    )
    projected, receipt = project_dossier(inputs["evidence"])
    inputs.update(evidence=projected, projection_receipt=receipt)
    calls = transport(monkeypatch, response(inputs))
    asyncio.run(module.final_review_inline(**inputs))
    delivered = delivered_evidence(calls[0]["messages"][1]["content"])
    assert delivered == projected
    assert {"prior_review", "shared_texts"} <= set(delivered)


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value["evidence"].pop("solver_1"),
        lambda value: value.update(deadline=time.time() - 1),
        lambda value: value.update(prior_review_charge_usd=3.0),
        lambda value: value["evidence"].update(shared_texts="unbound projection"),
    ],
)
def test_missing_evidence_or_exhausted_original_limits_do_not_call_model(
    inputs, monkeypatch, change
):
    calls = transport(monkeypatch, response(inputs))
    change(inputs)
    with pytest.raises((ValueError, TimeoutError, BudgetExceeded)):
        asyncio.run(module.final_review_inline(**inputs))
    assert not calls
