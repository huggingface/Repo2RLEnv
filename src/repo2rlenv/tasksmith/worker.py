from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError

from repo2rlenv.curation.agent import SHELL_TOOL, run_agent
from repo2rlenv.curation.budget import Budget, BudgetExceeded


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        ).encode()
    ).hexdigest()


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as stream:
        json.dump(value, stream, indent=2, default=str, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


async def artifact_stage[Artifact: BaseModel](
    *,
    schema: type[Artifact],
    stage: str,
    inputs: dict,
    system: str,
    prompt: str,
    root: Path,
    budget: Budget,
    model: str,
    runtime: str,
    max_cost: float,
    max_turns: int,
    deadline: float,
    shell: Callable[..., Awaitable[str]] | None = None,
    extra_tools: list[dict] | None = None,
    extra_handlers: dict[str, Callable[..., Awaitable[str]]] | None = None,
    validate: Callable[[Artifact], Awaitable[None]] | None = None,
) -> Artifact:
    """Commit typed output before graph advancement; never reroll unchanged completed stages.

    A crashed worker is deliberately not replayed blindly. If it submitted its artifact,
    recovery uses it. Otherwise its in-progress marker requires explicit reconciliation.
    """
    root.mkdir(parents=True, exist_ok=True)
    identity = canonical_digest(
        {
            "stage": stage,
            "inputs": inputs,
            "system": system,
            "prompt": prompt,
            "schema": schema.model_json_schema(),
            "model": model,
            "runtime": runtime,
        }
    )
    result_path, operation_path = root / "artifact.json", root / "operation.json"
    if result_path.exists():
        stored = json.loads(result_path.read_text())
        if stored["input_digest"] != identity:
            raise ValueError(f"{stage}: saved artifact belongs to different inputs")
        return schema.model_validate(stored["artifact"])
    prior = json.loads(operation_path.read_text()) if operation_path.exists() else None
    if prior:
        raise RuntimeError(
            f"{stage}: retained {prior['status']} worker needs reconciliation; "
            "preserve its trace, remote state and charges instead of restarting it"
        )
    if deadline <= time.time():
        raise TimeoutError("Candidate deadline exhausted")
    accepted: Artifact | None = None
    lock = asyncio.Lock()
    operation = {
        "input_digest": identity,
        "stage": stage,
        "status": "running",
        "started_at": time.time(),
        "deadline": deadline,
        "starting_spend": budget.spent,
    }
    save_json(operation_path, operation)

    async def submit_artifact(**payload) -> str:
        nonlocal accepted
        async with lock:
            if accepted is not None:
                return "Artifact already committed. Finish with a brief summary."
            try:
                value = schema.model_validate(payload)
                if validate:
                    await validate(value)
            except (ValidationError, ValueError) as exc:
                return (
                    f"Artifact rejected; correct the concrete issue and resubmit: {str(exc)[:6000]}"
                )
            save_json(
                result_path, {"input_digest": identity, "artifact": value.model_dump(mode="json")}
            )
            accepted = value
            return "Artifact committed. Do not call more tools; finish with a brief summary."

    async def remote_shell(command: str, timeout_sec: int = 120) -> str:
        if accepted is not None:
            return "Stage output committed; shell closed."
        remaining = int(deadline - time.time())
        if remaining <= 0:
            raise TimeoutError("Candidate deadline exhausted")
        return await shell(command=command, timeout_sec=min(timeout_sec, remaining))

    tools = [
        {
            "type": "function",
            "function": {
                "name": "submit_artifact",
                "description": "Validate and durably commit this stage's artifact.",
                "parameters": schema.model_json_schema(),
            },
        }
    ]
    handlers = {"submit_artifact": submit_artifact}
    if shell:
        tools.append(SHELL_TOOL)
        handlers["shell"] = remote_shell
    tools.extend(extra_tools or [])
    handlers.update(extra_handlers or {})
    try:
        async with asyncio.timeout(max(1, deadline - time.time())):
            await run_agent(
                model=model,
                system=system,
                prompt=prompt,
                budget=budget,
                tools=tools,
                handlers=handlers,
                trace=root / "trace.jsonl",
                max_turns=max_turns,
                max_cost=max_cost,
                runtime=runtime,
            )
        if accepted is None:
            raise ValueError(f"{stage}: worker ended without a validated artifact")
        operation["status"] = "completed"
        return accepted
    except BaseException as exc:
        operation["status"] = "completed" if accepted is not None else "incomplete"
        operation["error"] = f"{type(exc).__name__}: {exc}"
        if accepted is not None and isinstance(exc, (BudgetExceeded, ValueError)):
            return accepted
        raise
    finally:
        operation["finished_at"] = time.time()
        operation["charged_or_reserved_usd"] = budget.spent - operation["starting_spend"]
        save_json(operation_path, operation)
