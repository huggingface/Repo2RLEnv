from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace
from typing import TypedDict

import pytest
from pydantic import BaseModel

pytest.importorskip("langgraph.checkpoint.sqlite.aio")

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from repo2rlenv.curation import agent
from repo2rlenv.curation.budget import Budget
from repo2rlenv.tasksmith import worker


class Artifact(BaseModel):
    observation: str


class ParentState(TypedDict):
    result: str


def response(message):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(model_dump=lambda **_: message),
                finish_reason="tool_calls" if message.get("tool_calls") else "stop",
            )
        ],
        usage=SimpleNamespace(model_dump=lambda: {}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_first_attempt", [False, True])
async def test_parent_retry_runs_current_artifact_handler_even_with_old_nested_checkpoints(
    tmp_path, monkeypatch, legacy_first_attempt
):
    """Retry the same failed parent node across connections, with a fresh worker attempt."""
    database = tmp_path / "graph.sqlite"
    budget = Budget(tmp_path / "budget.json", 8)
    calls, validated = [], []
    attempt = 0
    original_compile = StateGraph.compile

    def compile_graph(self, *args, **kwargs):
        # Populate a real pre-fix nested checkpoint on attempt 0, then leave it in
        # SQLite while the corrected runtime resumes the same outer task ID.
        if legacy_first_attempt and attempt == 0 and kwargs.get("checkpointer") is False:
            kwargs["checkpointer"] = None
        return original_compile(self, *args, **kwargs)

    monkeypatch.setattr(StateGraph, "compile", compile_graph)

    async def completion(budget, model, messages, **kwargs):
        prompt = messages[1]["content"]
        assert prompt == f"Current attempt {attempt}"
        calls.append((attempt, prompt))
        if messages[-1]["role"] == "tool":
            assert "committed" in messages[-1]["content"]
            return response({"role": "assistant", "content": "Done."}), 0
        assert len(messages) == 2
        return response(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": f"submit-{attempt}",
                        "type": "function",
                        "function": {
                            "name": "submit_artifact",
                            "arguments": json.dumps({"observation": prompt}),
                        },
                    }
                ],
            }
        ), 0

    monkeypatch.setattr(agent, "completion", completion)

    async def validate_node(state):
        current = attempt

        async def validate(value):
            assert value.observation == f"Current attempt {current}"
            validated.append(current)

        value = await worker.artifact_stage(
            schema=Artifact,
            stage="public-comprehension",
            inputs={"attempt": current},
            system="Observe the current attempt.",
            prompt=f"Current attempt {current}",
            root=tmp_path / f"attempt-{current}",
            budget=budget,
            model="mock",
            runtime="langgraph",
            max_cost=1,
            max_turns=4,
            deadline=time.time() + 60,
            validate=validate,
        )
        if current == 0:
            raise RuntimeError("Interrupted after the first worker committed")
        return {"result": value.observation}

    def parent(saver):
        graph = StateGraph(ParentState)
        graph.add_node("validate", validate_node)
        graph.add_edge(START, "validate")
        graph.add_edge("validate", END)
        return graph.compile(checkpointer=saver)

    config = {"configurable": {"thread_id": "same-parent"}}
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        with pytest.raises(RuntimeError, match="Interrupted after"):
            await parent(saver).ainvoke({"result": ""}, config)
    first_artifact = (tmp_path / "attempt-0/artifact.json").read_bytes()
    with sqlite3.connect(database) as connection:
        nested_before = connection.execute(
            "SELECT checkpoint_ns, checkpoint_id, checkpoint FROM checkpoints "
            "WHERE checkpoint_ns != '' ORDER BY checkpoint_id"
        ).fetchall()
    assert bool(nested_before) is legacy_first_attempt

    attempt = 1
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        app = parent(saver)
        result = await app.ainvoke(None, config)
        assert result == {"result": "Current attempt 1"}
        assert await app.ainvoke(None, config) == result
    assert calls == [(0, "Current attempt 0")] * 2 + [(1, "Current attempt 1")] * 2
    assert validated == [0, 1]
    assert (tmp_path / "attempt-0/artifact.json").read_bytes() == first_artifact
    assert json.loads((tmp_path / "attempt-1/artifact.json").read_text())["artifact"] == {
        "observation": "Current attempt 1"
    }
    events = [
        json.loads(line) for line in (tmp_path / "attempt-1/trace.jsonl").read_text().splitlines()
    ]
    assert sum(event["kind"] == "model" for event in events) == 2
    assert sum(event["kind"] == "tool" for event in events) == 1
    assert budget.spent == 0
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT checkpoint_ns, checkpoint_id, checkpoint FROM checkpoints "
                "WHERE checkpoint_ns != '' ORDER BY checkpoint_id"
            ).fetchall()
            == nested_before
        )
