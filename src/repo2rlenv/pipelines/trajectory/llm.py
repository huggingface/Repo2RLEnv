"""The one LLM call the qa, build and extend families make: a structured-output request to a model.

`TrajectoryPipeline.__init__` calls `configure` with the generation input's `llm` and the
pipeline options (`max_llm_tokens`, `reasoning_effort`, `llm_cache_dir`, `max_llm_spend_usd`);
the call goes through repo2rlenv's LiteLLM client, so any provider it supports works.

Spend is the sum of `LLMResponse.cost_usd` over the calls this run made; cached replies are free.
Once it reaches the budget, `ask` raises BudgetExceeded before calling the model. Calls already
in flight still finish, so a run can overshoot by up to `jobs` calls.

The model only proposes (questions, contracts, tests); nothing it writes ships without passing
the checks in Docker. Responses are cached on disk, so re-running the pipeline is free and stable.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pydantic

from repo2rlenv.llm import complete
from repo2rlenv.spec.input import LLMSpec

SPEC: LLMSpec | None = None
MODEL = "none"  # recorded in task metadata (tests_written_by); set with SPEC
CACHE_DIR = Path("workspace/cache/trajectory_to_env/llm")
# Reasoning models count their thinking against max_tokens: too small a budget ends in an empty answer.
MAX_OUTPUT_TOKENS = 32000
REASONING_EFFORT: str | None = None
BUDGET_USD: float | None = None  # None: unlimited
SPENT_USD = 0.0
_SPEND_LOCK = threading.Lock()


class BudgetExceeded(Exception):
    pass


def configure(
    spec: LLMSpec | None,
    *,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    reasoning_effort: str | None = None,
    cache_dir: Path = CACHE_DIR,
    budget_usd: float | None = None,
) -> None:
    global SPEC, MODEL, MAX_OUTPUT_TOKENS, REASONING_EFFORT, CACHE_DIR, BUDGET_USD, SPENT_USD
    SPEC, MODEL = spec, spec.qualified_name if spec else "none"
    MAX_OUTPUT_TOKENS, REASONING_EFFORT = max_tokens, reasoning_effort
    CACHE_DIR = Path(cache_dir).expanduser()
    BUDGET_USD, SPENT_USD = budget_usd, 0.0


def spent_usd() -> float:
    with _SPEND_LOCK:
        return SPENT_USD


def strict_schema(node):
    """Strict json_schema output requires additionalProperties: false on every object."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for value in node.values():
            strict_schema(value)
    elif isinstance(node, list):
        for value in node:
            strict_schema(value)
    return node


def ask(system_prompt: str, user_prompt: str, response_schema):
    """Return a validated `response_schema` instance, or None if the model declined."""
    if SPEC is None:
        raise RuntimeError(
            "this family needs an LLM: set `llm` in the generation config or pass --llm"
        )
    key = hashlib.sha256(
        f"{MODEL}\n{response_schema.__name__}\n{system_prompt}\n{user_prompt}".encode()
    ).hexdigest()
    cached = CACHE_DIR / f"{key}.json"
    if cached.exists():
        return response_schema.model_validate_json(cached.read_text())

    global SPENT_USD
    with _SPEND_LOCK:
        if BUDGET_USD is not None and SPENT_USD >= BUDGET_USD:
            raise BudgetExceeded(f"${SPENT_USD:.2f} spent, budget ${BUDGET_USD:.2f}")
    response = complete(
        SPEC,
        system=system_prompt,
        user=user_prompt,
        max_tokens=MAX_OUTPUT_TOKENS,
        reasoning_effort=REASONING_EFFORT,
        response_schema=strict_schema(response_schema.model_json_schema()),
    )
    with _SPEND_LOCK:
        SPENT_USD += response.cost_usd
    content = (
        response.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    )
    try:
        parsed = response_schema.model_validate_json(content)
    except pydantic.ValidationError as error:
        # truncated, or a provider that ignores response_format: a decline
        failure = {"usage": response.usage, "error": str(error)[:2000], "content": content}
        (CACHE_DIR / "declined").mkdir(parents=True, exist_ok=True)
        (
            CACHE_DIR / "declined" / f"{hashlib.sha256(user_prompt.encode()).hexdigest()}.json"
        ).write_text(json.dumps(failure, default=str))
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_text(parsed.model_dump_json())
    return parsed
