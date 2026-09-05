from __future__ import annotations

import hashlib
import json

MAX_OUTPUT_TOKENS = 16000
MODEL_TIMEOUT_SEC = 300


def anthropic_options(model: str) -> dict:
    adaptive_models = (
        "claude-sonnet-4-6",
        "claude-sonnet-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
    )
    if model.startswith("anthropic/") and model.split("/", 1)[1].startswith(adaptive_models):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}
    return {}


def inference_settings(model: str) -> dict:
    return {"model": model, "max_tokens": MAX_OUTPUT_TOKENS, **anthropic_options(model)}


def inference_digest(model: str) -> str:
    return hashlib.sha256(
        json.dumps(inference_settings(model), sort_keys=True).encode()
    ).hexdigest()
