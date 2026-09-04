from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypedDict

from repo2rlenv.curation.budget import Budget, completion


class State(TypedDict):
    messages: list[dict]
    turns: int
    cost: float


SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Execute a bash command in the remote sandbox. Use heredocs to edit files.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


async def run_agent(
    *,
    model: str,
    system: str,
    prompt: str,
    budget: Budget,
    tools: list[dict],
    handlers: dict[str, Callable[..., Awaitable[str]]],
    trace: Path,
    max_turns: int,
    max_cost: float = 8,
) -> State:
    """A real LangGraph model/tool loop; tool effects remain in cloud sandboxes."""
    from langgraph.graph import END, START, StateGraph

    trace.parent.mkdir(parents=True, exist_ok=True)
    start_spend = budget.spent

    def record(kind: str, data: dict) -> None:
        with trace.open("a") as f:
            f.write(json.dumps({"kind": kind, **data}, default=str) + "\n")

    async def think(state: State) -> State:
        if budget.spent - start_spend >= max_cost:
            raise RuntimeError(f"Agent cost limit reached: ${max_cost}")
        response, cost = await completion(budget, model, state["messages"], tools=tools)
        message = response.choices[0].message.model_dump(exclude_none=True)
        record(
            "model",
            {
                "turn": state["turns"],
                "message": message,
                "cost_usd": cost,
                "usage": response.usage.model_dump(),
            },
        )
        return {
            "messages": [*state["messages"], message],
            "turns": state["turns"] + 1,
            "cost": state["cost"] + cost,
        }

    async def act(state: State) -> State:
        messages = list(state["messages"])
        for call in messages[-1].get("tool_calls", []):
            name = call["function"]["name"]
            try:
                arguments = json.loads(call["function"]["arguments"])
                if name not in handlers:
                    raise ValueError(f"Unknown tool: {name}")
                output = await handlers[name](**arguments)
            except (ValueError, TypeError, KeyError) as exc:
                output = f"Tool input error: {exc}"
            output = (
                output
                if len(output) <= 24000
                else output[:8000] + "\n[truncated]\n" + output[-16000:]
            )
            record("tool", {"name": name, "call_id": call["id"], "output": output})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
        return {**state, "messages": messages}

    def route(state: State) -> str:
        return "act" if state["messages"][-1].get("tool_calls") else END

    graph = StateGraph(State)
    graph.add_node("think", think)
    graph.add_node("act", act)
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", route)
    graph.add_conditional_edges("act", lambda state: "think" if state["turns"] < max_turns else END)
    initial: State = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "turns": 0,
        "cost": 0,
    }
    record("input", {"system": system, "prompt": prompt, "model": model})
    return await graph.compile().ainvoke(initial, {"recursion_limit": max_turns * 2 + 5})
