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
        return {"usage": self.usage}

    async def iter_any(self):
        events = [
            {"type": "message_start", "message": {"usage": self.usage}},
            {"type": "message_delta", "usage": {"output_tokens": 50}},
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
    async with make_bridge(tmp_path, max_cost=0.075) as bridge:
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
        return RuntimeProcess(json.loads(Path(args[2]).read_text()))

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
