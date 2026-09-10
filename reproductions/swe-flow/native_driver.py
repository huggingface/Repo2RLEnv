"""Use original SWE-Flow stages and FluxLLM with bounded, recorded completions."""

import importlib
import os
import sys
from pathlib import Path

from api_usage import record_response
from openai.resources.chat.completions import AsyncCompletions, Completions

sync_create = Completions.create
async_create = AsyncCompletions.create


def bounded(kwargs):
    kwargs["max_tokens"] = min(kwargs.get("max_tokens") or 4096, 4096)
    kwargs["timeout"] = 180
    return kwargs


def measured_sync(self, *args, **kwargs):
    response = sync_create(self, *args, **bounded(kwargs))
    record_response(response)
    return response


async def measured_async(self, *args, **kwargs):
    response = await async_create(self, *args, **bounded(kwargs))
    record_response(response)
    return response


Completions.create = measured_sync
AsyncCompletions.create = measured_async

stage = sys.argv[1]
if stage not in {"docstring", "specification"}:
    raise ValueError(stage)
root = Path("/work/swe-flow/native-output")
(root / "cache").mkdir(exist_ok=True)
sys.argv = [
    "sweflow-create-" + stage,
    "--project-root",
    "/work/swe-flow/workspace",
    "--development-schedule",
    str(root / "development-schedule.json"),
    "--base-url",
    "https://api.openai.com/v1",
    "--api-key",
    os.environ["OPENAI_API_KEY"],
    "--model",
    "gpt-4o",
    "--max-qpm",
    "24",
    "--max-retries",
    "1",
    "--cache-file",
    str(root / "cache" / f"{stage}s.jsonl"),
    "--output-file",
    str(root / f"{stage}s.json"),
]
importlib.import_module("sweflow.extensions.python.create_" + stage).main()
