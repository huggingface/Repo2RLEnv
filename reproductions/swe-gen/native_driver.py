"""Original SWE-gen CLI with SDK resource caps and provider-response receipts."""

import dataclasses
import json
from pathlib import Path

from api_usage import record_response
from openai.resources.chat.completions import Completions

original_parse = Completions.parse


def measured_parse(self, *args, **kwargs):
    response = original_parse(self, *args, **kwargs)
    record_response(response)
    return response


Completions.parse = measured_parse

import swegen.create.claude_code_runner as native  # noqa: E402 - bind telemetry before native import

original_options = native.ClaudeAgentOptions
original_query = native.query


def bounded_options(*args, **kwargs):
    kwargs.update(max_budget_usd=8, max_turns=80)
    return original_options(*args, **kwargs)


async def measured_query(*args, **kwargs):
    destination = Path("/evidence/swe-gen/claude-messages.jsonl")
    async for message in original_query(*args, **kwargs):
        data = dataclasses.asdict(message) if dataclasses.is_dataclass(message) else str(message)
        with destination.open("a") as stream:
            stream.write(
                json.dumps({"type": type(message).__name__, "message": data}, default=str) + "\n"
            )
        yield message


native.ClaudeAgentOptions = bounded_options
native.query = measured_query

from swegen.cli import app  # noqa: E402 - native runner options must be patched first

app()
