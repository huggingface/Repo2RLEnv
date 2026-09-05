from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

from aiohttp import ClientSession, web

from repo2rlenv.curation import bridge as bridge_module
from repo2rlenv.curation import external_agent
from repo2rlenv.curation.bridge import AgentBridge, normalize_cache, usage_cost
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.inference import MAX_OUTPUT_TOKENS, MODEL_TIMEOUT_SEC, anthropic_options


def test_provider_usage_accounts_cache_without_double_charging():
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 200,
        "cache_creation": {"ephemeral_1h_input_tokens": 10},
    }
    assert usage_cost(usage, 1, 2) == pytest.approx(265)
    with pytest.raises(ValueError, match="incomplete usage"):
        usage_cost({"output_tokens": 50}, 1, 2)
    data = {"system": [{"text": "a", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}
    normalize_cache(data)
    assert "ttl" not in data["system"][0]["cache_control"]


@pytest.mark.asyncio
async def test_bridge_denies_unauthenticated_tools_and_unmatched_models(tmp_path):
    effects = []

    async def remote(command):
        effects.append(command)
        return "remote response"

    budget = Budget(tmp_path / "budget.json", 1)
    async with (
        AgentBridge(
            model="anthropic/claude-sonnet-5",
            budget=budget,
            tools=[{"function": {"name": "shell"}}],
            handlers={"shell": remote},
            trace=tmp_path / "trace.jsonl",
            max_turns=2,
            max_cost=1,
        ) as bridge,
        ClientSession() as client,
    ):
        async with client.post(bridge.url + "/tool", json={"name": "shell"}) as r:
            assert r.status == 401
        headers = {"Authorization": "Bearer " + bridge.token}
        async with client.post(
            bridge.url + "/tool",
            headers=headers,
            json={"name": "shell", "arguments": {"command": "pwd"}},
        ) as r:
            assert (await r.json())["output"] == "remote response"
        async with client.post(
            bridge.url + "/v1/messages", headers=headers, json={"model": "different-model"}
        ) as r:
            assert r.status == 502
        assert bridge.failed.is_set()
    assert effects == ["pwd"]
    assert budget.spent == 0


class BridgeRequest:
    def __init__(self, body, *, token="", parsed=None):
        self.body, self.parsed = body, parsed
        self.headers = {"x-api-key": token}

    async def json(self):
        # Both concurrent requests finish the admission preamble before either
        # JSON body is available, reproducing the original admission race.
        await asyncio.sleep(0)
        if self.parsed is not None:
            self.parsed.set()
        return self.body.copy()


class LocalProvider:
    """Deterministic provider responses; no upstream HTTP requests are made."""

    status = 200

    def __init__(self):
        self.requests = []
        self.called = asyncio.Event()
        self.finish = asyncio.Event()
        self.finish.set()
        self.usage = {"input_tokens": 100, "output_tokens": 50}
        self.blocks = [{"type": "text", "text": "Completed response"}]
        self.stop_reason = "end_turn"
        self.content = self

    def post(self, url, *, json, headers):
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["x-api-key"] == "dummy-provider-key"
        self.requests.append(json)
        self.called.set()
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def close(self):
        pass

    async def json(self):
        await self.finish.wait()
        return {"usage": self.usage, "content": self.blocks, "stop_reason": self.stop_reason}

    async def iter_any(self):
        events = [
            {"type": "message_start", "message": {"usage": self.usage}},
            *[
                {"type": "content_block_start", "index": i, "content_block": block}
                for i, block in enumerate(self.blocks)
            ],
            {
                "type": "message_delta",
                "usage": {"output_tokens": 50},
                "delta": {"stop_reason": self.stop_reason},
            },
            {"type": "message_stop"},
        ]
        yield "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
        await self.finish.wait()


@pytest.fixture
def local_provider(monkeypatch):
    import litellm

    provider = LocalProvider()
    monkeypatch.setattr(bridge_module, "ClientSession", lambda **kwargs: provider)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-provider-key")
    monkeypatch.setattr(
        litellm,
        "get_model_info",
        lambda model: {"input_cost_per_token": 0.00001, "output_cost_per_token": 0.00002},
    )
    return provider


def make_bridge(tmp_path, **overrides):
    config = {
        "model": "anthropic/claude-mock",
        "budget": Budget(tmp_path / "budget.json", 10),
        "tools": [],
        "handlers": {},
        "trace": tmp_path / "trace.jsonl",
        "max_turns": 2,
        "max_cost": 1,
    }
    return AgentBridge(**(config | overrides))


def model_request():
    return BridgeRequest({"model": "claude-mock", "messages": [], "max_tokens": 100})


@pytest.mark.asyncio
async def test_model_admission_enforces_turn_limit_after_reading_concurrent_bodies(
    tmp_path, local_provider
):
    async with make_bridge(tmp_path, max_turns=1) as bridge:
        replies = await asyncio.gather(
            bridge._messages(model_request()),
            bridge._messages(model_request()),
            return_exceptions=True,
        )
    assert sum(isinstance(reply, BudgetExceeded) for reply in replies) == 1
    assert len(local_provider.requests) == bridge.turns == 1


@pytest.mark.asyncio
async def test_agent_cost_limit_includes_the_next_reservation(tmp_path, local_provider):
    async with make_bridge(tmp_path, max_cost=0.01) as bridge:
        with pytest.raises(BudgetExceeded, match="Runtime agent budget"):
            await bridge._messages(model_request())
    assert bridge.budget.spent == 0
    assert bridge.turns == 0
    assert local_provider.requests == []


@pytest.mark.asyncio
async def test_agent_cost_limit_counts_an_inflight_model_reservation(tmp_path, local_provider):
    local_provider.finish.clear()
    async with make_bridge(tmp_path, max_cost=0.5) as bridge:
        first = asyncio.create_task(bridge._messages(model_request()))
        try:
            await asyncio.wait_for(local_provider.called.wait(), 2)
            with pytest.raises(BudgetExceeded, match="Runtime agent budget"):
                await bridge._messages(model_request())
        finally:
            local_provider.finish.set()
            await first
    assert len(local_provider.requests) == 1
    assert bridge.budget.spent == pytest.approx(0.002)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget_options",
    [{"limit": 0.01}, {"limit": 10, "scope": "candidate", "scope_limit": 0.01}],
)
async def test_bridge_keeps_global_and_candidate_reservation_limits(
    tmp_path, local_provider, budget_options
):
    budget = Budget(tmp_path / "limited.json", **budget_options)
    async with make_bridge(tmp_path, budget=budget) as bridge:
        with pytest.raises(BudgetExceeded):
            await bridge._messages(model_request())
    assert budget.spent == 0
    assert local_provider.requests == []


@pytest.mark.asyncio
async def test_cancelling_bridge_owner_cancels_active_and_queued_tool_effects(tmp_path):
    started, cancelled, queued, ready = (asyncio.Event() for _ in range(4))
    effects = []

    async def remote(command):
        started.set()
        try:
            await asyncio.Event().wait()
            effects.append(command)
        finally:
            cancelled.set()

    bridge = make_bridge(tmp_path, handlers={"shell": remote})

    async def owner():
        async with bridge:
            ready.set()
            await asyncio.Event().wait()

    owner_task = asyncio.create_task(owner())
    await asyncio.wait_for(ready.wait(), 2)
    active = asyncio.create_task(
        bridge.tool(
            BridgeRequest({"name": "shell", "arguments": {"command": "first"}}, token=bridge.token)
        )
    )
    waiting = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        waiting = asyncio.create_task(
            bridge.tool(
                BridgeRequest(
                    {"name": "shell", "arguments": {"command": "second"}},
                    token=bridge.token,
                    parsed=queued,
                )
            )
        )
        await asyncio.wait_for(queued.wait(), 2)
    finally:
        owner_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner_task, 2)
        await asyncio.wait_for(
            asyncio.gather(active, *([waiting] if waiting else []), return_exceptions=True), 2
        )
    assert cancelled.is_set()
    assert active.cancelled() and waiting.cancelled()
    assert effects == []
    with pytest.raises(web.HTTPServiceUnavailable):
        await bridge.tool(BridgeRequest({"name": "shell", "arguments": {}}, token=bridge.token))


@pytest.mark.asyncio
async def test_tool_failure_rejects_queued_effects_and_preserves_the_first_error(tmp_path):
    started, release, queued = (asyncio.Event() for _ in range(3))
    effects = []

    async def remote(command):
        if command == "first":
            started.set()
            await release.wait()
            raise RuntimeError("candidate deferred")
        effects.append(command)
        return "unexpected queued effect"

    async with make_bridge(tmp_path, handlers={"shell": remote}) as bridge:
        first = asyncio.create_task(
            bridge.tool(
                BridgeRequest(
                    {"name": "shell", "arguments": {"command": "first"}}, token=bridge.token
                )
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(
            bridge.tool(
                BridgeRequest(
                    {"name": "shell", "arguments": {"command": "second"}},
                    token=bridge.token,
                    parsed=queued,
                )
            )
        )
        await asyncio.wait_for(queued.wait(), 2)
        release.set()
        replies = await asyncio.gather(first, second, return_exceptions=True)
    assert replies[0].status == 409
    assert isinstance(replies[1], asyncio.CancelledError)
    assert str(bridge.failure) == "candidate deferred"
    assert effects == []


@pytest.fixture
def local_runtime(monkeypatch, tmp_path, local_provider):
    """An in-process runtime exits at message_stop while provider EOF is pending."""
    local_provider.finish.clear()

    class DrainBridge(AgentBridge):
        async def __aexit__(self, *args):
            local_provider.finish.set()
            return await super().__aexit__(*args)

    class RuntimeProcess:
        returncode = None

        def __init__(self, config):
            self.config = config

        async def communicate(self):
            async with ClientSession() as client:
                async with client.post(
                    self.config["bridge_url"] + "/v1/messages",
                    headers={"x-api-key": self.config["bridge_token"]},
                    json={"model": self.config["model"], "messages": [], "stream": True},
                ) as response:
                    assert response.status == 200
                    async for line in response.content:
                        if b"message_stop" in line:
                            break
            self.returncode = 0
            return json.dumps({"messages": [], "turns": 1, "cost": 0}).encode(), None

    async def create_runtime(*args, **kwargs):
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        config = json.loads(Path(args[2]).read_text())
        assert config["max_tokens"] == MAX_OUTPUT_TOKENS
        assert config["model_timeout_sec"] == MODEL_TIMEOUT_SEC
        assert config["inference_options"] == anthropic_options("anthropic/" + config["model"])
        return RuntimeProcess(config)

    monkeypatch.setattr(external_agent, "AgentBridge", DrainBridge)
    monkeypatch.setattr(external_agent, "runtime_path", lambda engine: tmp_path / "runtime.mjs")
    monkeypatch.setattr(external_agent.asyncio, "create_subprocess_exec", create_runtime)


@pytest.mark.asyncio
async def test_runtime_result_cost_waits_for_stream_settlement(
    tmp_path, local_provider, local_runtime
):
    budget = Budget(tmp_path / "budget.json", 10)
    result = await external_agent.run_external_agent(
        engine="pi",
        model="anthropic/claude-mock",
        system="system",
        prompt="prompt",
        budget=budget,
        tools=[],
        handlers={},
        trace=tmp_path / "trace.jsonl",
        max_turns=1,
    )
    assert result["cost"] == budget.spent == pytest.approx(0.002)
    assert result["turns"] == 1


@pytest.mark.asyncio
async def test_runtime_propagates_accounting_errors_after_message_stop(
    tmp_path, local_provider, local_runtime
):
    local_provider.usage = {"output_tokens": 50}
    budget = Budget(tmp_path / "budget.json", 10)
    with pytest.raises(ValueError, match="incomplete usage"):
        await external_agent.run_external_agent(
            engine="pi",
            model="anthropic/claude-mock",
            system="system",
            prompt="prompt",
            budget=budget,
            tools=[],
            handlers={},
            trace=tmp_path / "trace.jsonl",
            max_turns=1,
        )
    assert budget.spent > 0
    ledger = json.loads(budget.path.read_text())
    assert {entry["status"] for entry in ledger["entries"].values()} == {"reserved"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-4-6", "claude-mock"])
async def test_bridge_overrides_runtime_inference_settings_and_preserves_opaque_thinking(
    tmp_path, local_provider, model
):
    opaque = [
        {"type": "thinking", "thinking": "", "signature": "opaque-signed-reasoning"},
        {"type": "redacted_thinking", "data": "opaque-redacted-reasoning"},
        {"type": "text", "text": "Previous response"},
    ]
    async with make_bridge(tmp_path, model="anthropic/" + model) as bridge:
        body = {
            "model": model,
            "messages": [{"role": "assistant", "content": opaque}],
            "max_tokens": 1,
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "high"},
            "temperature": 0.3,
            "top_p": 0.1,
            "top_k": 2,
        }
        await bridge._messages(BridgeRequest(body))
    forwarded = local_provider.requests[0]
    assert forwarded["max_tokens"] == MAX_OUTPUT_TOKENS
    assert forwarded["messages"][0]["content"] == opaque
    assert not {"temperature", "top_p", "top_k"} & forwarded.keys()
    for key in ("thinking", "output_config"):
        assert forwarded.get(key) == anthropic_options("anthropic/" + model).get(key)
    events = [json.loads(line) for line in bridge.trace.read_text().splitlines()]
    assert events[0]["inference"]["max_tokens"] == MAX_OUTPUT_TOKENS
    assert events[0]["timeout_sec"] == MODEL_TIMEOUT_SEC
    assert events[0]["tool_choice"] == forwarded["tool_choice"] == {"type": "auto"}
    assert events[0]["inference"].get("thinking") == forwarded.get("thinking")


@pytest.fixture
def in_memory_sse(monkeypatch):
    class StreamResponse:
        def __init__(self, **kwargs):
            pass

        async def prepare(self, request):
            pass

        async def write(self, chunk):
            pass

        async def write_eof(self):
            pass

    monkeypatch.setattr(bridge_module.web, "StreamResponse", StreamResponse)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["max_tokens", "length", "empty", "thinking_only"])
async def test_empty_and_truncated_provider_turns_fail_after_metering_and_trace(
    tmp_path, local_provider, in_memory_sse, stream, failure
):
    if failure in {"max_tokens", "length"}:
        local_provider.stop_reason = failure
    else:
        local_provider.blocks = (
            []
            if failure == "empty"
            else [{"type": "thinking", "thinking": "", "signature": "retained-opaque-signature"}]
        )
    async with make_bridge(tmp_path) as bridge:
        reply = await bridge.messages(
            BridgeRequest(
                {"model": "claude-mock", "messages": [], "stream": stream},
                token=bridge.token,
            )
        )
        assert reply.status == 502
        assert bridge.failed.is_set()
        assert bridge.budget.spent == bridge.cost == pytest.approx(0.002)
    events = [json.loads(line) for line in bridge.trace.read_text().splitlines()]
    assert [event["kind"] for event in events] == ["model_request", "model", "runtime_error"]
    assert events[1]["cost_usd"] == pytest.approx(0.002)
    assert events[1]["usage"]["output_tokens"] == 50
    if failure == "thinking_only":
        assert "retained-opaque-signature" in json.dumps(events[1])
    assert (
        "truncated" in events[2]["error"]
        if failure in {"max_tokens", "length"}
        else "no text or tool calls" in events[2]["error"]
    )
    ledger = json.loads(bridge.budget.path.read_text())
    assert {entry["status"] for entry in ledger["entries"].values()} == {"metered"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_tool_only_provider_turn_is_usable(tmp_path, local_provider, in_memory_sse, stream):
    local_provider.blocks = [{"type": "tool_use", "id": "tool-1", "name": "shell", "input": {}}]
    local_provider.stop_reason = "tool_use"
    async with make_bridge(tmp_path) as bridge:
        await bridge._messages(
            BridgeRequest({"model": "claude-mock", "messages": [], "stream": stream})
        )
        assert bridge.failure is None
        assert bridge.cost == pytest.approx(0.002)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["tool_use", "max_tokens"])
async def test_tool_waits_for_delayed_model_eof_and_validation(
    tmp_path, local_provider, in_memory_sse, stop_reason
):
    local_provider.finish.clear()
    local_provider.stop_reason = stop_reason
    local_provider.blocks = [{"type": "tool_use", "id": "tool-1", "name": "shell", "input": {}}]
    effects, observed_costs = [], []
    parsed = asyncio.Event()

    async def shell():
        effects.append("executed")
        observed_costs.append(bridge.cost)
        return "done"

    async with make_bridge(tmp_path, handlers={"shell": shell}) as bridge:
        model = asyncio.create_task(
            bridge.messages(
                BridgeRequest(
                    {"model": "claude-mock", "stream": True},
                    token=bridge.token,
                )
            )
        )
        await asyncio.wait_for(local_provider.called.wait(), 2)
        tool = asyncio.create_task(
            bridge.tool(
                BridgeRequest(
                    {"name": "shell", "arguments": {}},
                    token=bridge.token,
                    parsed=parsed,
                )
            )
        )
        try:
            await asyncio.wait_for(parsed.wait(), 2)
            assert effects == []
            assert not tool.done()
            assert bridge.cost == 0
        finally:
            local_provider.finish.set()
            replies = await asyncio.wait_for(asyncio.gather(model, tool, return_exceptions=True), 2)
        assert bridge.cost == pytest.approx(0.002)
        if stop_reason == "max_tokens":
            assert effects == []
            assert isinstance(replies[1], asyncio.CancelledError)
            assert "truncated" in str(bridge.failure)
        else:
            assert effects == ["executed"]
            assert observed_costs == [pytest.approx(0.002)]
            assert replies[1].status == 200
    records = [json.loads(line) for line in bridge.trace.read_text().splitlines()]
    assert [record["kind"] for record in records] == [
        "model_request",
        "model",
        "runtime_error" if stop_reason == "max_tokens" else "tool",
    ]


@pytest.mark.asyncio
async def test_cancelling_waiting_tool_does_not_cancel_model_settlement(
    tmp_path, local_provider, in_memory_sse
):
    local_provider.finish.clear()
    parsed = asyncio.Event()
    effects = []

    async def shell():
        effects.append("must not execute")
        return "unexpected"

    async with make_bridge(tmp_path, handlers={"shell": shell}) as bridge:
        model = asyncio.create_task(
            bridge.messages(
                BridgeRequest(
                    {"model": "claude-mock", "stream": True},
                    token=bridge.token,
                )
            )
        )
        await asyncio.wait_for(local_provider.called.wait(), 2)
        tool = asyncio.create_task(
            bridge.tool(
                BridgeRequest(
                    {"name": "shell", "arguments": {}},
                    token=bridge.token,
                    parsed=parsed,
                )
            )
        )
        try:
            await asyncio.wait_for(parsed.wait(), 2)
            tool.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(tool, 2)
            assert not model.done()
            assert bridge.failure is None
        finally:
            local_provider.finish.set()
            await asyncio.wait_for(model, 2)
        assert bridge.cost == pytest.approx(0.002)
        assert effects == []


@pytest.mark.asyncio
async def test_bridge_shutdown_cancels_model_and_gated_tool_without_deadlock(
    tmp_path, local_provider, in_memory_sse
):
    local_provider.finish.clear()
    ready, parsed = asyncio.Event(), asyncio.Event()
    effects = []

    async def shell():
        effects.append("must not execute")
        return "unexpected"

    bridge = make_bridge(tmp_path, handlers={"shell": shell})

    async def owner():
        async with bridge:
            ready.set()
            await asyncio.Event().wait()

    owning = asyncio.create_task(owner())
    await asyncio.wait_for(ready.wait(), 2)
    model = asyncio.create_task(
        bridge.messages(
            BridgeRequest(
                {"model": "claude-mock", "stream": True},
                token=bridge.token,
            )
        )
    )
    await asyncio.wait_for(local_provider.called.wait(), 2)
    tool = asyncio.create_task(
        bridge.tool(
            BridgeRequest(
                {"name": "shell", "arguments": {}},
                token=bridge.token,
                parsed=parsed,
            )
        )
    )
    try:
        await asyncio.wait_for(parsed.wait(), 2)
    finally:
        owning.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owning, 2)
        await asyncio.wait_for(asyncio.gather(model, tool, return_exceptions=True), 2)
    assert model.cancelled() and tool.cancelled()
    assert not bridge._model_tasks and not bridge._tool_tasks
    assert effects == []
    assert bridge.budget.spent > 0  # Uncertain provider work retains its reservation.


@pytest.mark.asyncio
async def test_actual_opencode_truncated_tool_response_cannot_execute_before_eof(
    tmp_path, local_provider, monkeypatch
):
    try:
        external_agent.runtime_path("opencode")
    except RuntimeError as exc:
        pytest.skip(str(exc))
    local_provider.finish.clear()
    arrived = asyncio.Event()
    effects = []
    bridges = []

    class ObservedBridge(AgentBridge):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            bridges.append(self)

        async def tool(self, request):
            arrived.set()
            return await super().tool(request)

    async def stream():
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "message-mock",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-mock",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 100, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-mock",
                    "name": "shell",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"command":"echo remote-only"}',
                },
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "max_tokens", "stop_sequence": None},
                "usage": {"output_tokens": 50},
            },
            {"type": "message_stop"},
        ]
        yield "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        ).encode()
        await local_provider.finish.wait()

    async def shell(command):
        effects.append(command)
        return "must not execute"

    monkeypatch.setattr(local_provider, "iter_any", stream)
    monkeypatch.setattr(external_agent, "AgentBridge", ObservedBridge)
    budget = Budget(tmp_path / "budget.json", 10)
    running = asyncio.create_task(
        external_agent.run_external_agent(
            engine="opencode",
            model="anthropic/claude-mock",
            system="Use the supplied remote tool.",
            prompt="Call shell once.",
            budget=budget,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "shell",
                        "description": "Run remotely",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    },
                }
            ],
            handlers={"shell": shell},
            trace=tmp_path / "trace.jsonl",
            max_turns=1,
        )
    )
    try:
        await asyncio.wait_for(arrived.wait(), 20)
        assert effects == []
        assert bridges[0].cost == 0
    finally:
        local_provider.finish.set()
        with pytest.raises(RuntimeError, match="truncated"):
            await asyncio.wait_for(running, 20)
    assert effects == []
    assert budget.spent == pytest.approx(0.002)
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert [event["kind"] for event in events] == [
        "input",
        "model_request",
        "model",
        "runtime_error",
    ]
    assert events[2]["cost_usd"] == pytest.approx(0.002)
