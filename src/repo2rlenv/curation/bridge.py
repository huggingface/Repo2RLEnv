from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.inference import (
    MAX_OUTPUT_TOKENS,
    MODEL_TIMEOUT_SEC,
    anthropic_options,
    inference_settings,
)


class AgentBridge:
    """Only model inference and explicitly registered remote tools cross this API.

    Runtime processes receive a short-lived loopback token, never provider keys.
    Requests are metered before forwarding; incomplete streams retain reservations.
    """

    def __init__(self, *, model, budget: Budget, tools, handlers, trace: Path, max_turns, max_cost):
        if not model.startswith("anthropic/"):
            raise ValueError("The matched runtime experiment currently supports Anthropic models")
        self.model, self.budget = model, budget
        self.tools, self.handlers, self.trace = tools, handlers, trace
        self.max_turns, self.max_cost = max_turns, max_cost
        self.turns, self.cost = 0, 0.0
        self.start_spend = budget.spent
        self.token = secrets.token_urlsafe(32)
        self.failure: BaseException | None = None
        self.failed = asyncio.Event()
        self.tool_lock = asyncio.Lock()
        self.model_lock = asyncio.Lock()
        self._closing = False
        self._tool_tasks: set[asyncio.Task] = set()
        self._model_tasks: set[asyncio.Task] = set()

    def record(self, kind, **data):
        with self.trace.open("a") as f:
            f.write(json.dumps({"kind": kind, **data}, default=str) + "\n")

    async def __aenter__(self):
        self.trace.parent.mkdir(parents=True, exist_ok=True)
        self.http = ClientSession(timeout=ClientTimeout(total=MODEL_TIMEOUT_SEC))
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_post("/v1/messages", self.messages)
        app.router.add_get("/v1/models", self.models)
        app.router.add_post("/tool", self.tool)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    def stop(self, *, cancel_models=False):
        """Reject new work and cancel tool effects before terminating the runtime."""
        self._closing = True
        tasks = self._tool_tasks | (self._model_tasks if cancel_models else set())
        current = asyncio.current_task()
        for task in tasks:
            if task is not current:
                task.cancel()

    async def __aexit__(self, exc_type, exc, tb):
        self.stop(cancel_models=exc_type is not None)
        try:
            # A runtime can exit as soon as it sees message_stop, before the
            # upstream stream reaches EOF and its final charge is recorded.
            await asyncio.gather(*self._tool_tasks, *self._model_tasks, return_exceptions=True)
        finally:
            try:
                await self.runner.cleanup()
            finally:
                await self.http.close()

    def ensure_open(self):
        if self._closing:
            raise web.HTTPServiceUnavailable(text="Runtime bridge is stopping")

    def authenticate(self, request):
        token = request.headers.get("x-api-key") or request.headers.get(
            "Authorization", ""
        ).removeprefix("Bearer ")
        if not secrets.compare_digest(token, self.token):
            raise web.HTTPUnauthorized()

    def fail(self, exc):
        if self.failure is None:
            self.failure = exc
            self.failed.set()
            self.record("runtime_error", error=f"{type(exc).__name__}: {exc}")
        self.stop(cancel_models=True)

    async def models(self, request):
        self.authenticate(request)
        return web.json_response(
            {"data": [{"id": self.model.split("/", 1)[1], "type": "model"}], "has_more": False}
        )

    async def tool(self, request):
        self.authenticate(request)
        self.ensure_open()
        preceding_models = tuple(self._model_tasks)
        task = asyncio.current_task()
        self._tool_tasks.add(task)
        try:
            data = await request.json()
            name, arguments = data.get("name"), data.get("arguments", {})
            if name not in self.handlers or not isinstance(arguments, dict):
                raise web.HTTPBadRequest(text="Unknown tool or malformed arguments")
            try:
                # Native runtimes can dispatch tools at message_stop before
                # provider EOF. Match LangGraph: meter and validate that model
                # response before allowing any effect. Cancelling a waiting
                # tool must not cancel a model that still needs settlement.
                await asyncio.gather(
                    *(asyncio.shield(model) for model in preceding_models),
                    return_exceptions=True,
                )
                async with self.tool_lock:
                    self.ensure_open()
                    output = await self.handlers[name](**arguments)
            except web.HTTPException:
                raise
            except (ValueError, TypeError, KeyError) as exc:
                output = f"Tool input error: {exc}"
            except Exception as exc:
                self.fail(exc)
                return web.json_response({"error": str(exc)}, status=409)
            if len(output) > 24000:
                output = output[:8000] + "\n[truncated]\n" + output[-16000:]
            self.record("tool", name=name, arguments=arguments, output=output)
            return web.json_response({"output": output})
        finally:
            self._tool_tasks.discard(task)

    async def messages(self, request):
        self.authenticate(request)
        self.ensure_open()
        task = asyncio.current_task()
        self._model_tasks.add(task)
        try:
            return await self._messages(request)
        except asyncio.CancelledError:
            self.fail(RuntimeError("Provider request cancelled before completion"))
            raise
        except web.HTTPException:
            raise
        except Exception as exc:
            self.fail(exc)
            return web.json_response(
                {"type": "error", "error": {"type": "api_error", "message": str(exc)}},
                status=429 if isinstance(exc, BudgetExceeded) else 502,
            )
        finally:
            self._model_tasks.discard(task)

    async def _messages(self, request):
        import litellm

        body = await request.json()
        model = self.model.split("/", 1)[1]
        if body.get("model") != model:
            raise ValueError("Runtime requested a different model than the matched experiment")
        allowed = {t["function"]["name"] for t in self.tools}
        if any(t.get("name") not in allowed for t in body.get("tools", [])):
            raise ValueError("Runtime exposed tools outside the matched allowlist")
        body["max_tokens"] = MAX_OUTPUT_TOKENS
        # Runtime defaults cannot change the matched inference policy. Preserve
        # signed/opaque thinking blocks in the conversation exactly as supplied.
        for key in ("thinking", "temperature", "top_p", "top_k", "output_config"):
            body.pop(key, None)
        body.update(anthropic_options(self.model))
        normalize_cache(body)
        pricing = litellm.get_model_info(self.model)
        input_rate, output_rate = pricing["input_cost_per_token"], pricing["output_cost_per_token"]
        if not input_rate or not output_rate:
            raise ValueError("Unknown model price")
        ceiling = (len(json.dumps(body).encode()) + 4096) * input_rate * 1.25 + body[
            "max_tokens"
        ] * output_rate
        async with self.model_lock:
            self.ensure_open()
            if self.turns >= self.max_turns:
                raise BudgetExceeded(f"Runtime model-call limit reached: {self.max_turns}")
            spent = self.budget.spent - self.start_spend
            if spent + ceiling > self.max_cost:
                raise BudgetExceeded(
                    f"Runtime agent budget ${self.max_cost}: ${spent:.4f} committed; "
                    f"need ${ceiling:.4f}"
                )
            reservation = self.budget.reserve(ceiling, f"llm:{self.model}")
            self.turns += 1
            turn = self.turns
        self.record(
            "model_request",
            turn=turn,
            model=model,
            system=body.get("system"),
            messages=body.get("messages", [])[-1:],
            tools=[t.get("name") for t in body.get("tools", [])],
            inference=inference_settings(self.model),
            timeout_sec=MODEL_TIMEOUT_SEC,
        )
        headers = {
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if request.headers.get("anthropic-beta"):
            headers["anthropic-beta"] = request.headers["anthropic-beta"]
        async with self.http.post(
            "https://api.anthropic.com/v1/messages", json=body, headers=headers
        ) as upstream:
            if upstream.status != 200:
                error = await upstream.text()
                if 400 <= upstream.status < 500:
                    self.budget.settle(reservation, 0)
                raise RuntimeError(f"Anthropic HTTP {upstream.status}: {error[:1500]}")
            if not body.get("stream"):
                result = await upstream.json()
                usage = result.get("usage", {})
                try:
                    cost = usage_cost(usage, input_rate, output_rate)
                except ValueError:
                    self.record(
                        "model",
                        turn=turn,
                        message=result,
                        usage=usage,
                        cost_usd=None,
                        reservation_usd=ceiling,
                    )
                    raise
                self.budget.settle(reservation, cost)
                self.cost += cost
                self.record("model", turn=turn, message=result, usage=usage, cost_usd=cost)
                validate_provider_response(result.get("content", []), result.get("stop_reason"))
                return web.json_response(result)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            events, pending = [], b""
            async for chunk in upstream.content.iter_any():
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.startswith(b"data: "):
                        with contextlib.suppress(ValueError):
                            events.append(json.loads(line[6:]))
                with contextlib.suppress(ConnectionError):
                    await response.write(chunk)
            usage = {}
            content = []
            stop_reason = None
            for event in events:
                if event.get("type") == "message_start":
                    message = event.get("message", {})
                    usage.update(message.get("usage", {}))
                    content.extend(message.get("content", []))
                    stop_reason = message.get("stop_reason") or stop_reason
                elif event.get("type") == "message_delta":
                    usage.update(event.get("usage", {}))
                    stop_reason = event.get("delta", {}).get("stop_reason") or stop_reason
                elif event.get("type") == "content_block_start":
                    content.append(event.get("content_block", {}))
                elif event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        content.append({"type": "text", "text": delta.get("text", "")})
            try:
                if not any(e.get("type") == "message_stop" for e in events):
                    raise RuntimeError("Incomplete provider stream; reservation retained")
                cost = usage_cost(usage, input_rate, output_rate)
            except (ValueError, RuntimeError):
                self.record(
                    "model",
                    turn=turn,
                    events=events,
                    usage=usage,
                    cost_usd=None,
                    reservation_usd=ceiling,
                )
                raise
            self.budget.settle(reservation, cost)
            self.cost += cost
            self.record("model", turn=turn, events=events, usage=usage, cost_usd=cost)
            validate_provider_response(content, stop_reason)
            with contextlib.suppress(ConnectionError):
                await response.write_eof()
            return response


def validate_provider_response(content, stop_reason):
    """A metered model turn needs usable text or a tool call, not just thinking."""
    if stop_reason in {"max_tokens", "length"}:
        raise RuntimeError(f"Provider response truncated: stop_reason={stop_reason}")
    if not any(
        (block.get("type") == "text" and (block.get("text") or "").strip())
        or block.get("type") == "tool_use"
        for block in content
    ):
        raise RuntimeError(
            "Provider response contained no text or tool calls (thinking-only or empty)"
        )


def normalize_cache(value):
    """Use the same five-minute cache pricing ceiling as the LangGraph baseline."""
    if isinstance(value, dict):
        if isinstance(value.get("cache_control"), dict):
            value["cache_control"] = {"type": "ephemeral"}
        for child in value.values():
            normalize_cache(child)
    elif isinstance(value, list):
        for child in value:
            normalize_cache(child)


def usage_cost(usage, input_rate, output_rate):
    if "input_tokens" not in usage or "output_tokens" not in usage:
        raise ValueError("Provider returned incomplete usage; retain reservation")
    cache = usage.get("cache_creation", {})
    long = cache.get("ephemeral_1h_input_tokens", 0)
    short = cache.get(
        "ephemeral_5m_input_tokens", max(0, usage.get("cache_creation_input_tokens", 0) - long)
    )
    return (
        usage["input_tokens"]
        + short * 1.25
        + long * 2
        + usage.get("cache_read_input_tokens", 0) * 0.1
    ) * input_rate + usage["output_tokens"] * output_rate
