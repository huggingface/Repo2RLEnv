from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import TypedDict

from repo2rlenv.curation.budget import Budget, BudgetExceeded, completion
from repo2rlenv.curation.inference import MAX_OUTPUT_TOKENS, inference_settings


class State(TypedDict):
    messages: list[dict]
    turns: int
    cost: float


class IncompleteModelResponse(RuntimeError):
    def __init__(self, message: str, state: State):
        super().__init__(message)
        self.state = state


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


def _validated_initial_state(state: State, system: str, prompt: str, max_turns: int) -> State:
    """Accept only a retained conversation at a fully completed tool-batch boundary."""
    state = deepcopy(state)
    if (
        type(state.get("turns")) is not int
        or not 0 < state["turns"] < max_turns
        or type(state.get("cost")) not in (int, float)
        or not math.isfinite(state["cost"])
        or state["cost"] < 0
    ):
        raise ValueError("Invalid retained turn/cost counters or exhausted turn allowance")
    messages = state.get("messages")
    if not isinstance(messages, list) or messages[:2] != [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]:
        raise ValueError("Retained conversation does not match system/prompt")
    pending, seen, turns = [], set(), 0
    for message in messages[2:]:
        if message.get("role") == "assistant":
            calls = message.get("tool_calls")
            if pending or not isinstance(calls, list) or not calls:
                raise ValueError("Resume requires complete tool batches, not final responses")
            turns += 1
            for call in calls:
                identity = call.get("id")
                if not isinstance(identity, str) or not identity or identity in seen:
                    raise ValueError("Invalid or duplicate retained tool call ID")
                if not isinstance(json.loads(call["function"]["arguments"]), dict):
                    raise ValueError("Retained tool arguments must be objects")
                pending.append(identity)
                seen.add(identity)
        elif message.get("role") == "tool":
            if (
                not pending
                or message.get("tool_call_id") != pending.pop(0)
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError("Missing, unexpected or out-of-order retained tool output")
        else:
            raise ValueError("Unexpected retained conversation role")
    if pending or turns != state["turns"] or messages[-1].get("role") != "tool":
        raise ValueError("Retained state must end after a complete tool batch")
    return state


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
    runtime: str = "langgraph",
    validate_final: Callable[[str], str | None] | None = None,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    initial_state: State | None = None,
) -> State:
    """A real LangGraph model/tool loop; tool effects remain in cloud sandboxes."""
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 128_000:
        raise ValueError("Output token limit must be an integer between 1 and 128000")
    if runtime != "langgraph":
        if initial_state is not None:
            raise ValueError("Retained-state continuation requires the LangGraph runtime")
        if validate_final is not None:
            raise ValueError("Final-response validation requires the LangGraph runtime")
        if max_output_tokens != MAX_OUTPUT_TOKENS:
            raise ValueError("Output token overrides require the LangGraph runtime")
        from repo2rlenv.curation.external_agent import run_external_agent

        return await run_external_agent(
            engine=runtime,
            model=model,
            system=system,
            prompt=prompt,
            budget=budget,
            tools=tools,
            handlers=handlers,
            trace=trace,
            max_turns=max_turns,
            max_cost=max_cost,
        )
    initial = (
        _validated_initial_state(initial_state, system, prompt, max_turns)
        if initial_state is not None
        else {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "turns": 0,
            "cost": 0,
        }
    )
    prior_cost = initial["cost"]
    from langgraph.graph import END, START, StateGraph

    trace.parent.mkdir(parents=True, exist_ok=True)
    start_spend = budget.spent

    def record(kind: str, data: dict) -> None:
        with trace.open("a") as f:
            f.write(json.dumps({"kind": kind, **data}, default=str) + "\n")

    record(
        "input",
        {
            "model": model,
            "system": system,
            "prompt": prompt,
            "runtime": runtime,
            "inference": inference_settings(model, max_tokens=max_output_tokens),
        },
    )

    async def think(state: State) -> State:
        if initial_state is not None and state["turns"] >= max_turns:
            raise BudgetExceeded(f"Agent turn limit reached: {max_turns}")
        used = prior_cost + budget.spent - start_spend
        if initial_state is not None:
            used = max(used, state["cost"])
        if used >= max_cost:
            raise BudgetExceeded(f"Agent cost limit reached: ${max_cost}")
        response, cost = await completion(
            budget,
            model,
            state["messages"],
            tools=tools,
            max_charge=max_cost - used,
            max_tokens=max_output_tokens,
        )
        message = response.choices[0].message.model_dump(exclude_none=True)
        record(
            "model",
            {
                "turn": state["turns"],
                "message": message,
                "cost_usd": cost,
                "usage": response.usage.model_dump(),
                "finish_reason": getattr(response.choices[0], "finish_reason", None),
            },
        )
        updated = {
            "messages": [*state["messages"], message],
            "turns": state["turns"] + 1,
            "cost": state["cost"] + cost,
        }
        if getattr(response.choices[0], "finish_reason", None) in {"length", "max_tokens"}:
            raise IncompleteModelResponse(
                "Incomplete model response: output token limit reached", updated
            )
        if not message.get("tool_calls") and not (message.get("content") or "").strip():
            raise IncompleteModelResponse(
                "Incomplete model response: no final text or tool call", updated
            )
        if not message.get("tool_calls") and validate_final is not None:
            correction = validate_final(message["content"])
            if correction is not None:
                record("final_validation", {"turn": updated["turns"], "feedback": correction})
                if updated["turns"] >= max_turns:
                    raise IncompleteModelResponse(
                        "Final-response requirements unmet at the turn limit: " + correction,
                        updated,
                    )
                # Preserve the premature response and continue the same bounded
                # conversation. This is not a fresh author/reviewer attempt.
                updated["messages"].append({"role": "user", "content": correction})
        return updated

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
        last = state["messages"][-1]
        if last.get("role") == "user":
            return "think"
        return "act" if last.get("tool_calls") else END

    graph = StateGraph(State)
    graph.add_node("think", think)
    graph.add_node("act", act)
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", route)
    graph.add_conditional_edges("act", lambda state: "think" if state["turns"] < max_turns else END)
    if initial_state is not None:
        record("continuation", {"prior_turns": initial["turns"], "prior_cost_usd": prior_cost})
    record("input", {"system": system, "prompt": prompt, "model": model})
    return await graph.compile().ainvoke(initial, {"recursion_limit": max_turns * 2 + 5})
