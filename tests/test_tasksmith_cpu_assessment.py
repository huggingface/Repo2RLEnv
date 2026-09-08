"""Real ledger transport, synthetic source evidence, and a mocked provider only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from repo2rlenv.curation.budget import BudgetExceeded, completion
from repo2rlenv.tasksmith import cpu_fixture as cpu
from tests.test_tasksmith_cpu_fixture import (
    arguments,
    delivered_evidence,
    structured_response,
    version_observation,
)
from tests.test_tasksmith_cpu_fixture import setup as _setup

setup = _setup


def transport(s, monkeypatch, *, response=None, error=None, inspect=lambda call: None):
    calls = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        inspect(kwargs)
        if error is not None:
            raise error
        return response or structured_response(s.assessment)

    monkeypatch.setattr(cpu, "completion", completion)
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            get_model_info=lambda _: {
                "input_cost_per_token": 0.000005,
                "output_cost_per_token": 0.000025,
            },
            acompletion=acompletion,
            completion_cost=lambda **kwargs: 0.11,
        ),
    )
    return calls


def prior(s, amount=0.8039835):
    key = s.budget.reserve(amount, "retained CPU assessment")
    s.budget.settle(key, amount)
    return {
        "prior_review_charge_usd": amount,
        "retained_review_evidence": {
            "trace": 'Earlier reasoning and counterevidence: "\\\x00🦊\n' * 200
        },
        "retained_review_context": {
            "original_operation": "synthetic/operation.json",
            "sha256": "a" * 64,
        },
    }


@pytest.mark.asyncio
async def test_one_complete_projected_request_preserves_evidence_and_original_allowance(
    setup, monkeypatch
):
    s = setup
    retained = prior(s)
    raw = cpu._evidence(
        s.request, retained["retained_review_evidence"], retained["retained_review_context"]
    )

    def inspect(call):
        inputs = json.loads((s.root / "inputs.json").read_text())
        delivered = delivered_evidence(call["messages"][1]["content"])
        assert cpu.restore_dossier(delivered, inputs["projection_receipt"]) == raw
        assert inputs["messages"] == call["messages"] and inputs["tools"] == call["tools"]
        assert inputs["prior_review_charge_usd"] == 0.8039835
        assert call["max_tokens"] == 3500 and call["tool_choice"] == "required"
        assert call["num_retries"] == 0

    calls = transport(s, monkeypatch, inspect=inspect)
    result = await cpu.review_cpu_fixture(s.request, **arguments(s), **retained)
    assert await cpu.review_cpu_fixture(s.request, **arguments(s), **retained) == result
    assert len(calls) == 1 and s.budget.spent == pytest.approx(0.8039835 + 0.11)
    assert (
        cpu.approved_runtime(s.request, **arguments(s), **retained)["transformers_version"]
        == "4.56.2"
    )
    receipt = json.loads((s.root / "delivery.json").read_text())
    supplied = delivered_evidence(calls[0]["messages"][1]["content"])
    assert receipt["evidence_sha256"] == {
        k: hashlib.sha256(v.encode()).hexdigest() for k, v in supplied.items()
    }
    assert receipt["original_evidence_sha256"] == {
        k: hashlib.sha256(v.encode()).hexdigest() for k, v in raw.items()
    }
    assert (
        receipt["response_sha256"]
        == hashlib.sha256((s.root / "response.json").read_bytes()).hexdigest()
    )
    phase = json.loads((s.root / "phase.json").read_text())
    assert phase["charged_or_reserved_usd"] == pytest.approx(0.11)
    (s.root / "response.json").write_text("changed")
    with pytest.raises(cpu.CpuFixtureUnsupported, match="evidence changed"):
        await cpu.review_cpu_fixture(s.request, **arguments(s), **retained)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["review", "candidate", "campaign", "global"])
async def test_reservation_cannot_bypass_original_limits_or_reroll(setup, monkeypatch, boundary):
    s = setup
    retained = prior(s, 2.99 if boundary == "review" else 0.8039835)
    if boundary == "candidate":
        s.budget.scope_limit = 0.81
    elif boundary == "campaign":
        s.budget.group_limit = 0.81
    elif boundary == "global":
        s.budget.limit = 0.81
    before = s.budget.spent
    calls = transport(s, monkeypatch)
    with pytest.raises(BudgetExceeded):
        await cpu.review_cpu_fixture(s.request, **arguments(s), **retained)
    assert not calls and s.budget.spent == before
    with pytest.raises(cpu.CpuFixtureUnsupported, match="reconciliation"):
        await cpu.review_cpu_fixture(s.request, **arguments(s), **retained)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["timeout", "cancel", "truncated", "wrong_tool", "bad_citation"])
async def test_incomplete_response_preserves_single_call_evidence_and_never_rerolls(
    setup, monkeypatch, kind
):
    s = setup
    error = (
        TimeoutError("provider stopped")
        if kind == "timeout"
        else asyncio.CancelledError()
        if kind == "cancel"
        else None
    )
    if kind == "bad_citation":
        s.assessment.evidence[-1].quote = "This quote was never observed."
    result = structured_response(
        s.assessment,
        finish="length" if kind == "truncated" else "tool_calls",
        name="wrong" if kind == "wrong_tool" else "emit_cpu_assessment",
    )
    calls = transport(s, monkeypatch, response=result, error=error)
    with pytest.raises((ValueError, TimeoutError, asyncio.CancelledError)):
        await cpu.review_cpu_fixture(s.request, **arguments(s))
    assert len(calls) == 1 and s.budget.spent > 0
    phase = json.loads((s.root / "phase.json").read_text())
    assert phase["status"] == "incomplete" and phase["charged_or_reserved_usd"] > 0
    if error is None:
        assert (s.root / "response.json").exists() and (s.root / "delivery.json").exists()
    else:
        ledger = json.loads(s.budget.path.read_text())
        assert {row["status"] for row in ledger["entries"].values()} == {"reserved"}
    with pytest.raises(cpu.CpuFixtureUnsupported, match="reconciliation"):
        await cpu.review_cpu_fixture(s.request, **arguments(s))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "missing_version",
        "missing_observation",
        "different_version",
        "hash",
        "timeout",
        "nonzero",
        "truncated",
        "unsupported",
        "oversized",
    ],
)
def test_version_requires_complete_supported_remote_observation(setup, change):
    data = setup.request.model_dump(mode="json")
    if change.startswith("missing_"):
        data.pop("transformers_version" if change == "missing_version" else "version_observation")
    elif change == "different_version":
        data["transformers_version"] = "4.57.6"
    elif change == "hash":
        data["version_observation"]["sha256"] = "f" * 64
    else:
        kwargs = (
            {"timed_out": True}
            if change == "timeout"
            else {"exit_code": 1}
            if change == "nonzero"
            else {"stdout": '{"version":'}
            if change == "truncated"
            else {"stdout": "x" * 4097}
            if change == "oversized"
            else {}
        )
        data["version_observation"] = version_observation(
            "4.57.7" if change == "unsupported" else "4.56.2", **kwargs
        )
    with pytest.raises(ValueError):
        cpu.CpuFixtureRequest.model_validate(data)
    assert not setup.calls


@pytest.mark.asyncio
async def test_prior_cost_requires_readable_evidence_and_bound_context(setup):
    s = setup
    with pytest.raises(ValueError, match="retained readable"):
        await cpu.review_cpu_fixture(s.request, **arguments(s), prior_review_charge_usd=0.1)
    assert not s.root.exists() and not s.calls
    retained = prior(s)
    await cpu.review_cpu_fixture(s.request, **arguments(s), **retained)
    retained["retained_review_context"]["sha256"] = "b" * 64
    with pytest.raises(cpu.CpuFixtureUnsupported, match="identity changed"):
        await cpu.review_cpu_fixture(s.request, **arguments(s), **retained)
    assert len(s.calls) == 1
