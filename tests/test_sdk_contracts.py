"""Exercise installed inference/Hub SDKs against loopback HTTP, without paid APIs."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from repo2rlenv.llm import complete
from repo2rlenv.spec.input import LLMSpec


@pytest.fixture
def sdk_server(monkeypatch):
    # Never forward a developer's credentials to the test endpoint.
    monkeypatch.setenv("OPENAI_API_KEY", "sdk-contract-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sdk-contract-key")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    state = {"requests": [], "status": 200, "body": {}, "headers": {}}

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            # HTTP field names are case-insensitive; SDKs may change their casing.
            headers = {name.lower(): value for name, value in self.headers.items()}
            state["requests"].append((self.command, self.path, body, headers))
            payload = state["body"]
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in state["headers"].items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_HEAD = do_POST = handle_request

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_completion_wire_contract_and_usage(sdk_server, provider):
    model = "gpt-4o-mini" if provider == "openai" else "claude-sonnet-4-6"
    if provider == "openai":
        sdk_server["body"] = {
            "id": "chatcmpl-contract",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
    else:
        sdk_server["body"] = {
            "id": "msg_contract",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
    endpoint = sdk_server["url"] + ("/v1" if provider == "openai" else "")
    spec = LLMSpec(
        provider=provider,
        model=model,
        endpoint=endpoint,
        api_key_env="OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY",
    )
    result = complete(spec, system="System", user="Hello", max_tokens=64)
    assert result.content == "Done"
    assert (result.prompt_tokens, result.completion_tokens) == (100, 20)
    assert result.cost_usd > 0
    assert len(sdk_server["requests"]) == 1
    method, path, data, headers = sdk_server["requests"][0]
    assert method == "POST"
    assert path == ("/v1/chat/completions" if provider == "openai" else "/v1/messages")
    if provider == "openai":
        assert headers["authorization"] == "Bearer sdk-contract-key"
    else:
        assert headers["x-api-key"] == "sdk-contract-key"
    request = json.loads(data)
    assert request["model"] == model
    assert request["max_tokens"] == 64
    content = "Hello" if provider == "openai" else [{"type": "text", "text": "Hello"}]
    assert request["messages"][-1] == {"role": "user", "content": content}


@pytest.mark.asyncio
async def test_async_responses_tool_arguments_and_usage(sdk_server):
    from openai import AsyncOpenAI

    arguments = '{"command":"printf hello"}'
    sdk_server["body"] = {
        "id": "resp_contract",
        "object": "response",
        "created_at": 1,
        "model": "gpt-5",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": "fc_contract",
                "call_id": "call_contract",
                "name": "shell",
                "arguments": arguments,
                "status": "completed",
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    }
    tool = {
        "type": "function",
        "name": "shell",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    }
    async with AsyncOpenAI(
        api_key="sdk-contract-key",
        base_url=sdk_server["url"] + "/v1",
        max_retries=0,
    ) as client:
        result = await client.responses.create(
            model="gpt-5",
            input="Say hello",
            tools=[tool],
            store=False,
            max_output_tokens=64,
        )
    assert result.output[0].arguments == arguments
    assert result.output[0].call_id == "call_contract"
    assert result.usage.input_tokens_details.cached_tokens == 30
    assert result.usage.output_tokens_details.reasoning_tokens == 5
    method, path, data, headers = sdk_server["requests"][0]
    assert (method, path) == ("POST", "/v1/responses")
    assert headers["authorization"] == "Bearer sdk-contract-key"
    assert json.loads(data)["tools"] == [tool]


def test_hub_download_transfers_bytes_and_pins_revision(sdk_server, tmp_path):
    from huggingface_hub import hf_hub_download

    revision = "a" * 40
    sdk_server["body"] = b"task bundle fixture\n"
    sdk_server["headers"] = {"X-Repo-Commit": revision, "ETag": '"' + "b" * 40 + '"'}
    path = hf_hub_download(
        "test/tasks",
        "task.toml",
        repo_type="dataset",
        revision=revision,
        endpoint=sdk_server["url"],
        cache_dir=tmp_path,
        token=False,
    )
    assert Path(path).read_bytes() == sdk_server["body"]
    assert [(method, path) for method, path, _, _ in sdk_server["requests"]] == [
        ("HEAD", f"/datasets/test/tasks/resolve/{revision}/task.toml"),
        ("GET", f"/datasets/test/tasks/resolve/{revision}/task.toml"),
    ]


def test_provider_auth_failure_is_not_retried(sdk_server):
    import litellm

    sdk_server["status"] = 401
    sdk_server["body"] = {"error": {"message": "Invalid key", "type": "authentication_error"}}
    spec = LLMSpec(provider="openai", model="gpt-4o-mini", endpoint=sdk_server["url"] + "/v1")
    with pytest.raises(litellm.AuthenticationError):
        complete(spec, user="Hello")
    assert len(sdk_server["requests"]) == 1
