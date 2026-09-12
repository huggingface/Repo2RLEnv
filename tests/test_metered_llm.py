from __future__ import annotations

import pytest
from pydantic import BaseModel

from repo2rlenv.campaigns.budget import BudgetLedger, OperationAlreadyRecorded
from repo2rlenv.campaigns.llm import metered_complete
from repo2rlenv.llm import LLMResponse
from repo2rlenv.spec.input import LLMSpec


def call(tmp_path, ledger):
    return metered_complete(
        LLMSpec(provider="test", model="test"),
        ledger=ledger,
        operation_id="issue:1",
        reservation_usd="0.50",
        receipt=tmp_path / "response.json",
        system="system",
        user="user",
        max_tokens=100,
    )


def test_request_timeout_holds_budget_and_prevents_redispatch(tmp_path, monkeypatch):
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="1")

    def timeout(*args, **kwargs):
        raise TimeoutError("Unknown provider outcome")

    monkeypatch.setattr("repo2rlenv.campaigns.llm.complete", timeout)
    with pytest.raises(TimeoutError):
        call(tmp_path, ledger)
    assert ledger.status()["reserved_usd"] == "0.500000"
    assert ledger.status()["operations"][0]["status"] == "uncertain"
    with pytest.raises(FileExistsError):
        call(tmp_path, ledger)
    (tmp_path / "response.json").unlink()
    with pytest.raises(OperationAlreadyRecorded):
        call(tmp_path, ledger)


@pytest.mark.parametrize(
    "cost,usage,settled",
    [(0.02, {"prompt_tokens": 2}, True), (0, {"prompt_tokens": 2}, False), (0.02, None, False)],
)
def test_only_evidenced_nonzero_estimates_release_reservation(
    tmp_path, monkeypatch, cost, usage, settled
):
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="1")
    monkeypatch.setattr(
        "repo2rlenv.campaigns.llm.complete",
        lambda *a, **kw: LLMResponse("result", usage=usage, cost_usd=cost),
    )
    assert call(tmp_path, ledger).content == "result"
    status = ledger.status()
    assert status["operations"][0]["status"] == ("settled" if settled else "uncertain")
    assert status["accounted_usd"] == ("0.020000" if settled else "0.000000")


def test_nested_provider_usage_is_persisted_without_losing_the_response(tmp_path, monkeypatch):
    import json

    class TokenDetails(BaseModel):
        reasoning_tokens: int

    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="1")
    response = LLMResponse(
        "report",
        usage={"completion_tokens_details": TokenDetails(reasoning_tokens=30)},
        cost_usd=0.02,
    )
    monkeypatch.setattr("repo2rlenv.campaigns.llm.complete", lambda *a, **kw: response)
    call(tmp_path, ledger)
    data = json.loads((tmp_path / "response.json").read_text())
    assert data["response"]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 30
    assert data["response"]["content"] == "report"


def test_resume_reuses_only_the_exact_request_without_charging_again(tmp_path, monkeypatch):
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="1")
    calls = []

    def complete(*args, **kwargs):
        calls.append(kwargs)
        return LLMResponse("report", usage={"prompt_tokens": 20}, cost_usd=0.02)

    monkeypatch.setattr("repo2rlenv.campaigns.llm.complete", complete)
    kwargs = dict(
        ledger=ledger,
        operation_id="resume:1",
        reservation_usd="0.50",
        receipt=tmp_path / "response.json",
        system="system",
        user="user",
        max_tokens=100,
    )
    model = LLMSpec(provider="test", model="test")
    metered_complete(model, **kwargs)
    assert metered_complete(model, **kwargs, resume=True).content == "report"
    assert len(calls) == 1
    assert ledger.status()["accounted_usd"] == "0.020000"
    kwargs["user"] = "changed"
    with pytest.raises(ValueError, match="different request"):
        metered_complete(model, **kwargs, resume=True)
