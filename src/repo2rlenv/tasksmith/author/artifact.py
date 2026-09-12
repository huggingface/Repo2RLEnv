from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path

from pydantic import BaseModel, ValidationError

from repo2rlenv.campaigns.budget import BudgetExceeded
from repo2rlenv.tasksmith.author.agent import SHELL_TOOL, run_agent
from repo2rlenv.tasksmith.author.budget import AuthorBudget as Budget

ARTIFACT_TOOL_PROTOCOL = 2
MAX_ARTIFACT_BYTES = 512_000
MAX_PATCH_BYTES = 64_000
MAX_JSON_DEPTH = 32


def _bounded_object(value: object, *, max_bytes: int) -> dict:
    """Validate JSON before recursive merging, and detach it from caller-owned objects."""
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"JSON nesting exceeds {MAX_JSON_DEPTH} levels")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("Only JSON values are supported")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > max_bytes:
        raise ValueError(f"JSON object exceeds {max_bytes} UTF-8 bytes")
    return json.loads(encoded)


def _merge_patch(target: object, patch: object) -> object:
    """RFC 7396 semantics; the public tool additionally requires an object root."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


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
    tools = [
        {
            "type": "function",
            "function": {
                "name": "submit_artifact",
                "description": "Submit a complete replacement artifact for validation and durable commit. After rejection, use revise_artifact to send only changed fields instead of repeating this full object.",
                "parameters": schema.model_json_schema(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "revise_artifact",
                "description": f"Correct the most recent rejected draft using a JSON merge patch (up to {MAX_PATCH_BYTES} UTF-8 bytes, {MAX_JSON_DEPTH} nesting levels). Omit unchanged fields. Objects merge recursively; null deletes a dictionary key; arrays replace entirely. All schema and operator validation runs again. A rejected revision becomes the next unvalidated draft. Use submit_artifact for a complete replacement or to assign a literal null value.",
                "parameters": {
                    "type": "object",
                    "properties": {"patch": {"type": "object", "additionalProperties": True}},
                    "required": ["patch"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    if shell:
        tools.append(SHELL_TOOL)
    tools.extend(extra_tools or [])
    names = [tool["function"]["name"] for tool in tools]
    if len(set(names)) != len(names) or set(extra_handlers or {}) & {
        "submit_artifact",
        "revise_artifact",
        "shell",
    }:
        raise ValueError("Extra tools/handlers cannot replace built-in artifact validation tools")
    legacy_inputs = {
        "stage": stage,
        "inputs": inputs,
        "system": system,
        "prompt": prompt,
        "schema": schema.model_json_schema(),
        "model": model,
        "runtime": runtime,
    }
    identity = canonical_digest(
        {
            **legacy_inputs,
            "tool_protocol": ARTIFACT_TOOL_PROTOCOL,
            "tools": tools,
            "artifact_limits": {
                "max_bytes": MAX_ARTIFACT_BYTES,
                "patch_bytes": MAX_PATCH_BYTES,
                "max_depth": MAX_JSON_DEPTH,
            },
        }
    )
    result_path, operation_path = root / "artifact.json", root / "operation.json"
    if result_path.exists():
        stored = json.loads(result_path.read_text())
        # Markerless completed artifacts retain their original identity contract.
        # They are not rewritten or silently upgraded to this new tool protocol.
        protocol = stored.get("tool_protocol", 1)
        expected = canonical_digest(legacy_inputs) if protocol == 1 else identity
        if protocol not in (1, ARTIFACT_TOOL_PROTOCOL) or stored["input_digest"] != expected:
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
    draft: dict | None = None
    draft_number = 0
    lock = asyncio.Lock()
    operation = {
        "input_digest": identity,
        "tool_protocol": ARTIFACT_TOOL_PROTOCOL,
        "stage": stage,
        "status": "running",
        "started_at": time.time(),
        "deadline": deadline,
        "starting_spend": budget.spent,
    }
    save_json(operation_path, operation)

    async def validate_and_commit(payload: dict, origin: str) -> str:
        nonlocal accepted, draft, draft_number
        try:
            payload = _bounded_object(payload, max_bytes=MAX_ARTIFACT_BYTES)
        except ValueError as exc:
            return f"Artifact rejected; existing draft unchanged: {exc}"
        draft, draft_number = payload, draft_number + 1
        retained = {
            "input_digest": identity,
            "tool_protocol": ARTIFACT_TOOL_PROTOCOL,
            "status": "unvalidated",
            "number": draft_number,
            "origin": origin,
            "artifact": draft,
        }
        save_json(root / "draft.json", retained)
        try:
            value = schema.model_validate(deepcopy(payload))
            if validate:
                await validate(value)
        except (ValidationError, ValueError) as exc:
            save_json(root / "draft.json", {**retained, "validation_error": str(exc)})
            return f"Artifact rejected; call revise_artifact with only changed fields, or submit_artifact for a complete replacement: {str(exc)[:6000]}"
        save_json(
            result_path,
            {
                "input_digest": identity,
                "tool_protocol": ARTIFACT_TOOL_PROTOCOL,
                "artifact": value.model_dump(mode="json"),
            },
        )
        accepted = value
        return "Artifact committed. Do not call more tools; finish with a brief summary."

    async def submit_artifact(**payload) -> str:
        async with lock:
            if accepted is not None:
                return "Artifact already committed. Finish with a brief summary."
            return await validate_and_commit(payload, "submit_artifact")

    async def revise_artifact(patch=None, **unexpected) -> str:
        async with lock:
            if accepted is not None:
                return "Artifact already committed. Finish with a brief summary."
            if draft is None:
                return "Artifact revision rejected: no prior draft. Call submit_artifact with a complete artifact first."
            try:
                if unexpected:
                    raise ValueError("revise_artifact accepts only the patch argument")
                patch = _bounded_object(patch, max_bytes=MAX_PATCH_BYTES)
            except ValueError as exc:
                return f"Artifact revision rejected; existing draft unchanged: {exc}"
            return await validate_and_commit(_merge_patch(draft, patch), "revise_artifact")

    async def remote_shell(command: str, timeout_sec: int = 120) -> str:
        if accepted is not None:
            return "Stage output committed; shell closed."
        remaining = int(deadline - time.time())
        if remaining <= 0:
            raise TimeoutError("Candidate deadline exhausted")
        return await shell(command=command, timeout_sec=min(timeout_sec, remaining))

    handlers = {"submit_artifact": submit_artifact, "revise_artifact": revise_artifact}
    if shell:
        handlers["shell"] = remote_shell
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
