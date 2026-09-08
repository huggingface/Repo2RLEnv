from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, Field

from repo2rlenv.curation import agent
from repo2rlenv.tasksmith import worker
from repo2rlenv.tasksmith.config import TasksmithConfig


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    source_paths: list[str] = Field(min_length=1)
    metadata: dict[str, str]
    checks: list[str]


def payload():
    return {
        "title": "Retain this detailed contract",
        "source_paths": ["src/pkg.py"],
        "metadata": {"keep": "yes", "obsolete": "old"},
        "checks": ["one", "two"],
    }


@pytest.fixture
def arguments(tmp_path):
    config = TasksmithConfig(
        ledger_path=tmp_path / "budget.json", ledger_limit_usd=100, campaign_id="worker-test"
    )
    return dict(
        schema=Manifest,
        stage="construction",
        inputs={"source_digest": "a" * 64},
        system="Produce a manifest.",
        prompt="Preserve the complete outcome.",
        root=tmp_path / "worker",
        budget=config.budget("candidate"),
        model="test-model",
        runtime="langgraph",
        max_cost=1,
        max_turns=8,
        deadline=time.time() + 60,
    )


@pytest.mark.asyncio
async def test_merge_patch_uses_real_agent_trace_and_same_validator_commit(arguments, monkeypatch):
    validated = []

    async def validate(value):
        validated.append(value.model_dump())
        if value.source_paths != ["src/pkg"]:
            raise ValueError("source_paths must collect the full src/pkg package")

    patch = {
        "source_paths": ["src/pkg"],
        "metadata": {"obsolete": None, "new": "value"},
        "checks": ["replacement"],
    }
    calls = [
        [("submit_artifact", payload())],
        [
            ("revise_artifact", {"patch": patch}),
            ("revise_artifact", {"patch": {"title": "must not replace committed title"}}),
        ],
        [],
    ]
    invocation = 0

    async def completion(budget, model, messages, **kwargs):
        nonlocal invocation
        assert budget is arguments["budget"] and model == "test-model"
        tools = {tool["function"]["name"]: tool["function"] for tool in kwargs["tools"]}
        description = tools["revise_artifact"]["description"]
        assert "Omit unchanged fields" in description and "arrays replace entirely" in description
        message = {"role": "assistant", "content": "Done."}
        if calls[invocation]:
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": f"{invocation}-{index}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(value)},
                    }
                    for index, (name, value) in enumerate(calls[invocation])
                ],
            }
        invocation += 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(model_dump=lambda **_: message),
                    finish_reason="tool_calls" if "tool_calls" in message else "stop",
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 0, "completion_tokens": 0}),
        ), 0

    monkeypatch.setattr(agent, "completion", completion)
    result = await worker.artifact_stage(**arguments, validate=validate)
    assert len(validated) == 2 and result.title == payload()["title"]
    assert result.metadata == {"keep": "yes", "new": "value"}
    assert result.checks == ["replacement"] and result.source_paths == ["src/pkg"]
    root = arguments["root"]
    stored = json.loads((root / "artifact.json").read_text())
    assert stored["tool_protocol"] == 2 and stored["artifact"] == result.model_dump()
    rows = [json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()]
    tools = [row for row in rows if row["kind"] == "tool"]
    assert [row["name"] for row in tools] == [
        "submit_artifact",
        "revise_artifact",
        "revise_artifact",
    ]
    assert tools[0]["output"].endswith("source_paths must collect the full src/pkg package")
    assert "committed" in tools[1]["output"] and "already committed" in tools[2]["output"]
    assert invocation == 3 and arguments["budget"].spent == 0


@pytest.mark.asyncio
async def test_rejected_revisions_are_latest_raw_draft_and_recheck_both_gates(
    arguments, monkeypatch
):
    seen = []

    async def validate(value):
        seen.append(value.model_dump())
        if "obsolete" in value.metadata:
            raise ValueError("Delete the obsolete metadata key")

    async def run(**call):
        submit, revise = (call["handlers"][name] for name in ("submit_artifact", "revise_artifact"))
        incomplete = payload()
        del incomplete["title"]
        assert "title" in await submit(**incomplete)
        assert not seen  # Pydantic must pass before the operator callback.
        assert "Delete the obsolete metadata key" in await revise(
            patch={"title": "Corrected", "source_paths": ["src/pkg"]}
        )
        draft = json.loads((arguments["root"] / "draft.json").read_text())
        assert draft["status"] == "unvalidated" and draft["number"] == 2
        assert draft["validation_error"] == "Delete the obsolete metadata key"
        assert not (arguments["root"] / "artifact.json").exists()
        assert "committed" in await revise(patch={"metadata": {"obsolete": None}})

    monkeypatch.setattr(worker, "run_agent", run)
    result = await worker.artifact_stage(**arguments, validate=validate)
    assert len(seen) == 2 and result.title == "Corrected" and result.source_paths == ["src/pkg"]
    assert result.metadata == {"keep": "yes"}


@pytest.mark.asyncio
async def test_no_prior_draft_and_complete_replacement_semantics(arguments, monkeypatch):
    async def validate(value):
        if value.title != "Complete replacement":
            raise ValueError("Replace title")

    async def run(**call):
        revise = call["handlers"]["revise_artifact"]
        assert "no prior draft" in await revise(patch={"title": "ignored"})
        assert not (arguments["root"] / "draft.json").exists()
        assert "rejected" in await call["handlers"]["submit_artifact"](**payload())
        replacement = {**payload(), "title": "Complete replacement", "metadata": {}}
        assert "committed" in await call["handlers"]["submit_artifact"](**replacement)

    monkeypatch.setattr(worker, "run_agent", run)
    result = await worker.artifact_stage(**arguments, validate=validate)
    assert result.metadata == {} and result.title == "Complete replacement"


def deep_object():
    value = {}
    for _ in range(worker.MAX_JSON_DEPTH + 1):
        value = {"nested": value}
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch,message",
    [
        (None, "JSON object"),
        ([{"title": "invalid"}], "JSON object"),
        (deep_object(), "nesting exceeds"),
        ({"title": "é" * worker.MAX_PATCH_BYTES}, "UTF-8 bytes"),
        ({"value": float("nan")}, "Out of range float"),
    ],
)
async def test_invalid_patch_leaves_draft_unchanged_and_cannot_commit(
    arguments, monkeypatch, patch, message
):
    validations = 0

    async def validate(value):
        nonlocal validations
        validations += 1
        raise ValueError("Operator gate is not satisfied")

    async def run(**call):
        assert "Operator gate" in await call["handlers"]["submit_artifact"](**payload())
        before = (arguments["root"] / "draft.json").read_bytes()
        assert message in await call["handlers"]["revise_artifact"](patch=patch)
        assert (arguments["root"] / "draft.json").read_bytes() == before

    monkeypatch.setattr(worker, "run_agent", run)
    with pytest.raises(ValueError, match="without a validated artifact"):
        await worker.artifact_stage(**arguments, validate=validate)
    assert validations == 1 and not (arguments["root"] / "artifact.json").exists()
    assert json.loads((arguments["root"] / "operation.json").read_text())["status"] == "incomplete"
    with pytest.raises(RuntimeError, match="needs reconciliation"):
        await worker.artifact_stage(**arguments, validate=validate)


@pytest.mark.asyncio
async def test_small_patch_cannot_grow_merged_artifact_past_bound(arguments, monkeypatch):
    monkeypatch.setattr(worker, "MAX_ARTIFACT_BYTES", 220)

    async def validate(value):
        raise ValueError("Not ready")

    async def run(**call):
        await call["handlers"]["submit_artifact"](**payload())
        before = (arguments["root"] / "draft.json").read_bytes()
        reply = await call["handlers"]["revise_artifact"](patch={"metadata": {"added": "x" * 200}})
        assert "220 UTF-8 bytes" in reply
        assert (arguments["root"] / "draft.json").read_bytes() == before

    monkeypatch.setattr(worker, "run_agent", run)
    with pytest.raises(ValueError, match="without a validated artifact"):
        await worker.artifact_stage(**arguments, validate=validate)


@pytest.mark.asyncio
async def test_legacy_completed_artifact_uses_exact_old_digest_without_rewrite(
    arguments, monkeypatch
):
    fields = {
        key: arguments[key] for key in ("stage", "inputs", "system", "prompt", "model", "runtime")
    }
    fields["schema"] = Manifest.model_json_schema()
    stored = {"input_digest": worker.canonical_digest(fields), "artifact": payload()}
    path = arguments["root"] / "artifact.json"
    worker.save_json(path, stored)
    before = path.read_bytes()

    async def forbidden(**kwargs):
        pytest.fail("Completed stage was rerolled")

    monkeypatch.setattr(worker, "run_agent", forbidden)
    assert (await worker.artifact_stage(**arguments)).model_dump() == payload()
    assert path.read_bytes() == before and not (arguments["root"] / "operation.json").exists()
    with pytest.raises(ValueError, match="different inputs"):
        await worker.artifact_stage(**{**arguments, "inputs": {"source_digest": "b" * 64}})
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_current_cache_binds_effective_tool_schema_and_rejects_unknown_protocol(
    arguments, monkeypatch
):
    async def run(**call):
        await call["handlers"]["submit_artifact"](**payload())

    monkeypatch.setattr(worker, "run_agent", run)
    first = await worker.artifact_stage(**arguments)

    async def forbidden(**kwargs):
        pytest.fail("Completed stage was rerolled")

    monkeypatch.setattr(worker, "run_agent", forbidden)
    assert await worker.artifact_stage(**arguments) == first
    extra = {
        "type": "function",
        "function": {"name": "new_evidence", "parameters": {"type": "object"}},
    }
    with pytest.raises(ValueError, match="different inputs"):
        await worker.artifact_stage(**arguments, extra_tools=[extra])
    path = arguments["root"] / "artifact.json"
    stored = json.loads(path.read_text())
    worker.save_json(path, {**stored, "tool_protocol": 999})
    with pytest.raises(ValueError, match="different inputs"):
        await worker.artifact_stage(**arguments)


@pytest.mark.asyncio
async def test_extra_handlers_cannot_bypass_shared_validation(arguments):
    async def substitute(**kwargs):
        return "committed"

    with pytest.raises(ValueError, match="cannot replace built-in"):
        await worker.artifact_stage(**arguments, extra_handlers={"revise_artifact": substitute})
    assert not (arguments["root"] / "operation.json").exists()


@pytest.mark.parametrize(
    "target,patch,expected",
    [
        ({"a": "b"}, {"a": "c"}, {"a": "c"}),
        ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
        ({"a": "b"}, {"a": None}, {}),
        ({"a": "b"}, {"missing": None}, {"a": "b"}),
        ({"a": ["b"]}, {"a": ["c", "d"]}, {"a": ["c", "d"]}),
        ({"a": "scalar"}, {"a": {"new": "value", "missing": None}}, {"a": {"new": "value"}}),
        ({"a": {"nested": "value"}}, {"a": "scalar"}, {"a": "scalar"}),
    ],
)
def test_merge_patch_dictionary_array_and_null_semantics(target, patch, expected):
    before = json.dumps(target)
    assert worker._merge_patch(target, patch) == expected
    assert json.dumps(target) == before
