"""Bounded Responses tool loop with controller-held credentials and durable costs.

Prices are standard tier USD per million tokens, verified 2026-09-25 at
https://developers.openai.com/api/docs/pricing. Keep requests below the long
context pricing threshold. One bounded regeneration of a known incomplete
response is allowed; transport failures retain their reservation. No fallback.
"""

from __future__ import annotations

import json
from decimal import Decimal
from inspect import signature

from repo2rlenv.campaigns.budget import BudgetExceeded
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.tasksmith.author.bridge import ProviderOutputError

PRICES = {
    "gpt-6-sol": (Decimal("2"), Decimal("0.20"), Decimal("10"), Decimal("2.50")),
    "gpt-6-luna": (Decimal("0.10"), Decimal("0.01"), Decimal("0.50"), Decimal("0.125")),
}
MAX_INPUT_BYTES = 240_000
MAX_OUTPUT_TOKENS = 8192


class AgentLimitReached(ValueError):
    """A bounded attempt ended with any existing workspace edits preserved."""


def model_name(model: str) -> str:
    name = model.removeprefix("openai/")
    if name not in PRICES:
        raise ValueError("This runtime requires openai/gpt-6-sol or openai/gpt-6-luna")
    return name


def usage_cost(model: str, usage: dict) -> Decimal:
    name = model_name(model)
    incoming, outgoing = usage["input_tokens"], usage["output_tokens"]
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    written = (usage.get("input_tokens_details") or {}).get("cache_write_tokens", 0)
    if any(type(n) is not int or n < 0 for n in (incoming, outgoing, cached, written)):
        raise ValueError("Provider usage must contain nonnegative integer token counts")
    if cached + written > incoming or incoming > 272_000:
        raise ValueError("Usage is outside the verified pricing contract")
    input_price, cache_price, output_price, write_price = PRICES[name]
    return (
        (incoming - cached - written) * input_price
        + cached * cache_price
        + written * write_price
        + outgoing * output_price
    ) / 1_000_000


async def run_openai_agent(
    *,
    model,
    system,
    prompt,
    budget,
    tools,
    handlers,
    trace,
    max_turns,
    max_cost=8,
    client=None,
    max_output_tokens=MAX_OUTPUT_TOKENS,
):
    name = model_name(model)
    if not 1 <= max_turns <= 100:
        raise ValueError("Agent turn limit must be between 1 and 100")
    if not 256 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise ValueError("Output allowance must be between 256 and 8192 tokens")
    from openai import APIStatusError, AsyncOpenAI

    own_client = client is None
    client = client or AsyncOpenAI(max_retries=0, timeout=180)
    trace.parent.mkdir(parents=True, exist_ok=True)
    requests = trace.with_suffix("")
    requests.mkdir(exist_ok=True)
    messages = [{"role": "user", "content": prompt}]
    functions = [{"type": "function", **tool["function"], "strict": False} for tool in tools]
    cost = Decimal(0)
    recovered = False

    def record(kind, **data):
        with trace.open("a") as stream:
            stream.write(json.dumps({"kind": kind, **data}) + "\n")

    try:
        record(
            "input",
            model=model,
            system=system,
            prompt=prompt,
            runtime="openai",
            reasoning_effort="medium",
            max_output_tokens=max_output_tokens,
        )
        for turn in range(1, max_turns + 1):
            request = dict(
                model=name,
                instructions=system,
                input=messages,
                tools=functions,
                tool_choice="required",
                parallel_tool_calls=False,
                store=False,
                include=["reasoning.encrypted_content"],
                reasoning={"effort": "medium"},
                service_tier="default",
                max_output_tokens=max_output_tokens,
            )
            # UTF-8 bytes upper-bound text tokens; include framing/tool overhead.
            input_bound = len(json.dumps(request, ensure_ascii=False).encode()) + 4096
            if input_bound > MAX_INPUT_BYTES:
                raise AgentLimitReached("Agent context reached its bounded input allowance")
            reservation = (
                input_bound * PRICES[name][3] + max_output_tokens * PRICES[name][2]
            ) / 1_000_000
            if cost + reservation > Decimal(str(max_cost)):
                raise BudgetExceeded("OpenAI stage cannot reserve another bounded turn")
            save_record(requests / f"{turn}-request.json", request)
            operation = budget.reserve(float(reservation), f"Responses {name} turn {turn}")
            record("model_request", turn=turn, operation=operation)
            response_path = requests / f"{turn}-response.json"
            try:
                response = await client.responses.create(**request)
                data = response.model_dump(mode="json")
                save_record(response_path, data)
                actual = usage_cost(name, data["usage"])
                budget.settle(operation, float(actual))
            except APIStatusError as exc:
                save_record(
                    response_path,
                    {
                        "status_code": exc.status_code,
                        "request_id": exc.request_id,
                        "error_type": type(exc).__name__,
                    },
                )
                if exc.status_code in {400, 401, 403, 404, 422}:
                    budget.settle(operation, 0)
                else:
                    budget.budget.mark_uncertain(operation, evidence=str(response_path))
                raise
            except BaseException:
                budget.budget.mark_uncertain(operation, evidence=str(response_path))
                raise
            cost += actual
            record("model_response", turn=turn, cost_usd=str(actual), response=str(response_path))
            if data.get("status") != "completed":
                reason = (data.get("incomplete_details") or {}).get("reason")
                if (
                    data.get("status") == "incomplete"
                    and reason in {"max_messages", "max_output_tokens"}
                    and not recovered
                    and turn < max_turns
                ):
                    # No tool from this incomplete response has executed. Keep
                    # its receipt and charge, but regenerate from the last
                    # complete conversation instead of replaying partial calls.
                    recovered = True
                    record("provider_recovery", turn=turn, reason=reason)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The last response stopped before completing a tool action. "
                                "None of its proposed actions ran. Choose the next supplied "
                                "tool now, or use the supplied completion tool if finished. "
                                "Keep the next action concise."
                            ),
                        }
                    )
                    continue
                raise ProviderOutputError(
                    f"Responses output incomplete: {data.get('incomplete_details')}"
                )
            # SDK output models contain optional null fields (e.g. reasoning
            # status) that the stateless input schema does not accept.
            messages.extend(
                {key: value for key, value in item.items() if value is not None}
                for item in data["output"]
            )
            calls = [item for item in data["output"] if item["type"] == "function_call"]
            if not calls:
                raise ProviderOutputError(
                    "Provider returned no tool call despite required tool choice"
                )
            for call in calls:
                if call["name"] not in handlers:
                    result = "Unknown tool; use only the tools supplied by the controller."
                else:
                    try:
                        arguments = json.loads(call["arguments"])
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be an object")
                    except ValueError as exc:
                        result = f"Invalid arguments: {exc}"
                    else:
                        handler = handlers[call["name"]]
                        try:
                            signature(handler).bind(**arguments)
                        except TypeError as exc:
                            # Reject malformed calls before any handler effect.
                            # Exceptions inside a handler remain real failures.
                            result = f"Invalid tool arguments: {exc}. Follow the supplied schema."
                        else:
                            record("tool_request", turn=turn, call=call)
                            result = await handler(**arguments)
                result = str(result)
                record("tool_result", call_id=call["call_id"], result=result)
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": result[:24000],
                    }
                )
                if result.startswith("Artifact committed."):
                    return {"messages": messages, "turns": turn, "cost": float(cost)}
        raise AgentLimitReached("OpenAI agent exhausted its turn allowance")
    finally:
        if own_client:
            await client.close()
