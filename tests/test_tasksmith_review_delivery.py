from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from repo2rlenv.curation import agent
from repo2rlenv.tasksmith import review, worker
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.models import PRIdentity


class Delivered(BaseModel):
    complete: bool


@pytest.mark.asyncio
async def test_escaped_evidence_survives_real_agent_transport_and_credits_only_delivered_ranges(
    tmp_path, monkeypatch
):
    # Each source character may occupy up to six characters in the tool's JSON.
    evidence = {"quoted": '"\\\n\t\x00' * 9000, "unicode": "αβγ\n" * 6000}
    reader = review._EvidenceReader(evidence, tmp_path)
    delivered = {key: [] for key in evidence}
    offsets = {key: 0 for key in evidence}
    seen, wire_outputs, shortened = set(), [], []
    counter = 0

    async def completion(budget, model, messages, **kwargs):
        nonlocal counter
        names = {
            call["id"]: call["function"]["name"]
            for message in messages
            for call in message.get("tool_calls", [])
        }
        for message in messages:
            if message["role"] != "tool" or message["tool_call_id"] in seen:
                continue
            seen.add(message["tool_call_id"])
            if names[message["tool_call_id"]] != "read_evidence":
                assert "committed" in message["content"]
                continue
            wire = message["content"]
            assert len(wire) <= 24_000
            assert "[truncated]" not in wire
            result = json.loads(wire)  # The original transport bug produces invalid JSON here.
            wire_outputs.append(wire)
            for page in result["pages"]:
                key, start, end = page["evidence_id"], page["offset"], page["next_offset"]
                assert start == offsets[key]
                assert page["text"] == evidence[key][start:end]
                assert 0 < end - start <= 12000
                shortened.append(end - start < min(12000, len(evidence[key]) - start))
                offsets[key] = end
                delivered[key].append([start, end])
        # Compare what the model actually received after agent.act with the durable receipt.
        assert reader.spans == delivered
        if wire_outputs:
            stored = json.loads(reader.path.read_text())
            assert stored["spans"] == delivered and stored["policy_version"] == 2
        if any(offsets[key] < len(text) for key, text in evidence.items()):
            requests = []
            for key, text in evidence.items():
                # Ask for several pages at once. Shortened pages must be followed
                # from returned offsets, not assumed requested endpoints.
                for start in range(offsets[key], min(len(text), offsets[key] + 24000), 12000):
                    requests.append({"evidence_id": key, "offset": start, "length": 12000})
            name, arguments = "read_evidence", {"requests": requests}
        elif not any(name == "submit_artifact" for name in names.values()):
            name, arguments = "submit_artifact", {"complete": True}
        else:
            message = {"role": "assistant", "content": "Complete."}
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(model_dump=lambda **_: message),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(model_dump=lambda: {}),
            ), 0
        counter += 1
        message = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"read-{counter}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(model_dump=lambda **_: message),
                    finish_reason="tool_calls",
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: {}),
        ), 0

    async def validate(value):
        reader.require_complete()
        assert value.complete

    monkeypatch.setattr(agent, "completion", completion)
    config = TasksmithConfig(
        ledger_path=tmp_path / "budget.json", ledger_limit_usd=10, campaign_id="delivery-test"
    )
    result = await worker.artifact_stage(
        schema=Delivered,
        stage="delivery",
        inputs={"evidence": evidence},
        system="Read all evidence.",
        prompt="Read complete source text and submit.",
        root=tmp_path / "stage",
        budget=config.budget("candidate"),
        model="mock-model",
        runtime="langgraph",
        max_cost=1,
        max_turns=30,
        deadline=time.time() + 60,
        extra_tools=[reader.tool],
        extra_handlers={"read_evidence": reader.read},
        validate=validate,
    )
    assert result.complete and len(wire_outputs) > 2 and any(shortened)
    assert offsets == {key: len(text) for key, text in evidence.items()}
    assert {
        key: "".join(evidence[key][a:b] for a, b in spans) for key, spans in delivered.items()
    } == evidence
    reader.require_complete()
    review._EvidenceReader(evidence, tmp_path).require_complete()
    traced = [
        json.loads(line) for line in (tmp_path / "stage/trace.jsonl").read_text().splitlines()
    ]
    assert [
        row["output"] for row in traced if row["kind"] == "tool" and row["name"] == "read_evidence"
    ] == wire_outputs


def test_policy_one_coverage_is_not_reused_or_rewritten_before_model_call(tmp_path, monkeypatch):
    evidence = {key: '"\\\n' * 100 for key in review.FINAL_REQUIRED_EVIDENCE}
    root = tmp_path / "review"
    receipt = {
        "policy_version": 1,
        "evidence_digest": worker.canonical_digest(evidence),
        "spans": {key: [[0, len(text)]] for key, text in evidence.items()},
    }
    worker.save_json(root / "read-coverage.json", receipt)
    before = (root / "read-coverage.json").read_bytes()

    async def forbidden(**kwargs):
        pytest.fail("An old potentially overcredited receipt reached model dispatch")

    monkeypatch.setattr(worker, "run_agent", forbidden)
    config = TasksmithConfig(
        ledger_path=tmp_path / "budget.json", ledger_limit_usd=10, campaign_id="delivery-test"
    )
    with pytest.raises(ValueError, match="different evidence/policy"):
        asyncio.run(
            review.final_review(
                config=config,
                budget=config.budget("candidate"),
                root=root,
                deadline=time.time() + 60,
                pr=PRIdentity(repository="example/library", number=1),
                revision_digest="a" * 64,
                evidence=evidence,
            )
        )
    assert (root / "read-coverage.json").read_bytes() == before
    assert not (root / "operation.json").exists()


def test_oversized_progress_metadata_is_shortened_without_omitting_delivered_evidence(tmp_path):
    evidence = {"normal": "Read this complete observation."}
    evidence.update({f"long-{i}-" + '"\\' * 1500: "Still unread" for i in range(16)})
    reader = review._EvidenceReader(evidence, tmp_path)
    wire = asyncio.run(reader.read(requests=[{"evidence_id": "normal"}]))
    assert len(wire) <= 24_000
    payload = json.loads(wire)
    assert payload["pages"][0]["text"] == evidence["normal"]
    assert payload["missing_count"] == 16 and payload["missing_list_truncated"]
    assert len(payload["missing"]) < 16
    assert reader.spans["normal"] == [[0, len(evidence["normal"])]]
    assert all(not spans for key, spans in reader.spans.items() if key != "normal")


def test_unrepresentable_page_metadata_never_credits_a_read(tmp_path):
    key = "x" * 24_000
    reader = review._EvidenceReader({key: "text"}, tmp_path)
    answer = asyncio.run(reader.read(requests=[{"evidence_id": key}]))
    assert "no read was credited" in answer and len(answer) < 24_000
    assert reader.spans == {key: []} and not reader.path.exists()
