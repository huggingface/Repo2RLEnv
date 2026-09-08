from __future__ import annotations

import asyncio
import hashlib
import json
import time

import pytest

from repo2rlenv.tasksmith.evidence_projection import restore_dossier
from repo2rlenv.tasksmith.validation import _review_dossier
from tests.test_tasksmith_inline_review import delivered_evidence, response, transport
from tests.test_tasksmith_inline_review import inputs as _inline_inputs


@pytest.fixture
def review(tmp_path):
    inputs = _inline_inputs.__wrapped__(tmp_path)
    inputs.pop("prior_review_charge_usd")
    texts = inputs.pop("evidence")
    source = 'Complete captured source: "\\\x00🦊\n' * 100
    texts["submissions"] = json.dumps({"first": source, "second": source}, indent=2)
    artifacts = dict(texts)

    def evidence(name, text):
        artifacts[name] = text

    return {**inputs, "texts": texts, "evidence": evidence}, artifacts


def test_normal_review_retains_raw_dossier_and_delivers_a_verified_reversible_projection(
    review, monkeypatch
):
    inputs, artifacts = review
    raw = dict(inputs["texts"])
    calls = transport(monkeypatch, response(inputs))
    report = asyncio.run(_review_dossier(**inputs))
    assert {key: artifacts[key] for key in raw} == raw
    metadata = json.loads(artifacts["final_review_input"])
    assert metadata["raw_sha256"] == {
        key: hashlib.sha256(text.encode()).hexdigest() for key, text in raw.items()
    }
    projected = metadata["projected_evidence"]
    assert restore_dossier(projected, metadata["projection_receipt"]) == raw
    assert "shared_texts" in projected
    assert delivered_evidence(calls[0]["messages"][1]["content"]) == projected
    delivery = json.loads(artifacts["final_review_delivery"])
    assert delivery["operation"]["status"] == "completed"
    assert delivery["operation"]["prior_review_charge_usd"] == 0
    assert set(delivery["files"]) == {
        "inputs.json",
        "response.json",
        "delivery.json",
        "quality-report.json",
    }
    for name, entry in delivery["files"].items():
        assert entry["text"].encode() == (inputs["root"] / name).read_bytes()
        assert entry["sha256"] == hashlib.sha256(entry["text"].encode()).hexdigest()
    assert json.loads(delivery["files"]["quality-report.json"]["text"]) == report.model_dump(
        mode="json"
    )
    assert inputs["budget"].spent == pytest.approx(0.11)


def test_invalid_review_retains_complete_delivery_without_a_verdict_or_a_second_request(
    review, monkeypatch
):
    inputs, artifacts = review
    calls = transport(monkeypatch, response(inputs, finish="length"))
    with pytest.raises(ValueError, match="complete structured"):
        asyncio.run(_review_dossier(**inputs))
    delivery = json.loads(artifacts["final_review_delivery"])
    assert delivery["operation"]["status"] == "incomplete"
    assert delivery["operation"]["charged_or_reserved_usd"] == pytest.approx(0.11)
    assert set(delivery["files"]) == {"inputs.json", "response.json", "delivery.json"}
    assert not (inputs["root"] / "quality-report.json").exists()
    with pytest.raises(ValueError, match="no reroll"):
        asyncio.run(_review_dossier(**inputs))
    assert len(calls) == 1
    assert json.loads(artifacts["final_review_delivery"]) == delivery


def test_expired_original_deadline_retains_input_but_never_fabricates_delivery(review, monkeypatch):
    inputs, artifacts = review
    inputs["deadline"] = time.time() - 1
    calls = transport(monkeypatch, response(inputs))
    with pytest.raises(TimeoutError, match="Original candidate deadline"):
        asyncio.run(_review_dossier(**inputs))
    assert "final_review_input" in artifacts
    assert "final_review_delivery" not in artifacts
    assert not calls and inputs["budget"].spent == 0


@pytest.mark.parametrize("change", ["missing", "unexpected"])
def test_wrong_raw_dossier_cannot_reach_projection_or_inference(review, monkeypatch, change):
    inputs, artifacts = review
    if change == "missing":
        inputs["texts"].pop("adversary")
    else:
        inputs["texts"]["uncited_extra"] = "Unexpected extra role"
    calls = transport(monkeypatch, response(inputs))
    with pytest.raises(ValueError, match="fourteen raw evidence roles"):
        asyncio.run(_review_dossier(**inputs))
    assert "final_review_input" not in artifacts
    assert not calls and inputs["budget"].spent == 0
