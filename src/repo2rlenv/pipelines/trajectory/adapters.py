"""Harness-native session logs -> ATIF-v1.7 trajectories.

Each adapter takes the parsed JSONL rows of one session and returns an ATIF dict.
Only what the log actually recorded is emitted; missing fields stay absent rather
than being guessed (atif-scan treats absent as unknown, not clean).
"""

from __future__ import annotations

import json
import re
from typing import Any

ATIF_VERSION = "ATIF-v1.7"
PI_FILE_ATTACHMENT = re.compile(
    r'<file name="[^"]*">\n?(.*?)\n?</file>(\n</file>)*', re.S
)  # pi's @file, user content


def _text_of(content: Any) -> str:
    """Flatten a message/tool-result content field (str or list of blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif block.get("type") in ("text", "input_text", "output_text"):
            parts.append(block.get("text", ""))
        elif block.get("type") == "tool_reference":
            parts.append(f"[tool_reference: {block.get('tool_name')}]")
        elif block.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _final_metrics(steps: list[dict]) -> dict:
    totals = {"total_prompt_tokens": 0, "total_completion_tokens": 0, "total_cached_tokens": 0}
    for step in steps:
        m = step.get("metrics") or {}
        totals["total_prompt_tokens"] += m.get("prompt_tokens", 0)
        totals["total_completion_tokens"] += m.get("completion_tokens", 0)
        totals["total_cached_tokens"] += m.get("cached_tokens", 0)
    totals["total_steps"] = len(steps)
    return totals


def _attach_result(steps: list[dict], call_id: str, content: str, is_error: bool = False) -> bool:
    for step in reversed(steps):
        if any(c["tool_call_id"] == call_id for c in step.get("tool_calls", [])):
            result = {"source_call_id": call_id, "content": content}
            if is_error:
                result["extra"] = {"is_error": True}
            step.setdefault("observation", {"results": []})["results"].append(result)
            return True
    return False


def _renumber(steps: list[dict]) -> list[dict]:
    for i, step in enumerate(steps, 1):
        step["step_id"] = i
        if step.get("timestamp") is None:
            step.pop("timestamp", None)
    return [{"step_id": s.pop("step_id"), **s} for s in steps]


# --------------------------------------------------------------------------- Claude Code


def claude_code_to_atif(rows: list[dict], session_ref: str) -> dict:
    """Claude Code session JSONL -> ATIF.

    Claude Code writes one line per content block, so consecutive assistant lines that
    share `message.id` are one model call and become one agent step. Tool results arrive
    as `user` lines and are attached to the step that issued the call. Sidechain
    (subagent) lines are dropped: they belong in their own trajectory.
    """
    steps: list[dict] = []
    model = version = session_id = None
    current_msg_id = None
    orphans = 0

    for row in rows:
        if row.get("type") not in ("user", "assistant") or row.get("isSidechain"):
            continue
        session_id = session_id or row.get("sessionId")
        version = version or row.get("version")
        msg = row.get("message") or {}
        ts = row.get("timestamp")

        if row["type"] == "assistant":
            model = model or msg.get("model")
            if msg.get("id") != current_msg_id:
                current_msg_id = msg.get("id")
                steps.append({"source": "agent", "timestamp": ts, "model_name": msg.get("model")})
            step = steps[-1]
            for block in msg.get("content") or []:
                kind = block.get("type")
                if kind == "text" and block.get("text"):
                    step["message"] = (step.get("message", "") + "\n" + block["text"]).strip()
                elif kind == "thinking" and block.get("thinking"):
                    step["reasoning_content"] = (
                        step.get("reasoning_content", "") + "\n" + block["thinking"]
                    ).strip()
                elif kind == "tool_use":
                    step.setdefault("tool_calls", []).append(
                        {
                            "tool_call_id": block["id"],
                            "function_name": block["name"],
                            "arguments": block.get("input") or {},
                        }
                    )
            usage = msg.get("usage")
            if usage:  # repeated on every block of the call; the last one is final
                cache_read = usage.get("cache_read_input_tokens", 0)
                step["metrics"] = {
                    "prompt_tokens": usage.get("input_tokens", 0)
                    + cache_read
                    + usage.get("cache_creation_input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "cached_tokens": cache_read,
                }
            continue

        current_msg_id = None
        content = msg.get("content")
        if isinstance(content, list) and any(
            b.get("type") == "tool_result" for b in content if isinstance(b, dict)
        ):
            for block in content:
                if block.get("type") == "tool_result":
                    ok = _attach_result(
                        steps,
                        block["tool_use_id"],
                        _text_of(block.get("content")),
                        bool(block.get("is_error")),
                    )
                    orphans += not ok
            continue
        if row.get("isMeta"):
            continue
        text = _text_of(content)
        if text.strip():
            steps.append({"source": "user", "timestamp": ts, "message": text})

    for step in steps:
        step.setdefault("message", "")
    steps = _renumber(steps)
    return {
        "schema_version": ATIF_VERSION,
        "session_id": session_id or session_ref,
        "agent": {"name": "claude-code", "version": version or "unknown", "model_name": model},
        "steps": steps,
        "final_metrics": _final_metrics(steps),
        "extra": {"source_ref": session_ref, "adapter": "claude_code", "orphan_results": orphans},
    }


# --------------------------------------------------------------------------- Codex


def codex_to_atif(rows: list[dict], session_ref: str) -> dict:
    """Codex rollout JSONL -> ATIF.

    `response_item`s carry the conversation; `event_msg` duplicates user messages (skipped)
    and carries per-call `token_count`s, attached to the latest agent step. Reasoning
    summaries are buffered onto the next agent step. Function calls issued back to back
    (no output in between) are one model turn, so they share a step.
    """
    steps: list[dict] = []
    meta: dict = {}
    model = None
    pending_reasoning: list[str] = []
    open_agent_step = False
    orphans = 0

    def agent_step(ts: str | None) -> dict:
        nonlocal open_agent_step
        if not open_agent_step:
            steps.append({"source": "agent", "timestamp": ts, "model_name": model})
            open_agent_step = True
        step = steps[-1]
        if pending_reasoning:
            step["reasoning_content"] = "\n".join(pending_reasoning)
            pending_reasoning.clear()
        return step

    for row in rows:
        kind, p, ts = row.get("type"), row.get("payload") or {}, row.get("timestamp")
        if kind == "session_meta":
            meta = p
            if p.get("base_instructions"):
                text = p["base_instructions"]
                text = text.get("text", "") if isinstance(text, dict) else text
                # No timestamp: session_meta's clock is not the event clock (seen 1h apart).
                steps.append({"source": "system", "message": text})
        elif kind == "turn_context":
            model = p.get("model") or model
        elif kind == "event_msg" and p.get("type") == "token_count":
            last = (p.get("info") or {}).get("last_token_usage")
            agent_steps = [s for s in steps if s["source"] == "agent"]
            if last and agent_steps and "metrics" not in agent_steps[-1]:
                agent_steps[-1]["metrics"] = {
                    "prompt_tokens": last.get("input_tokens", 0),
                    "completion_tokens": last.get("output_tokens", 0),
                    "cached_tokens": last.get("cached_input_tokens", 0),
                    "extra": {"reasoning_tokens": last.get("reasoning_output_tokens", 0)},
                }
        elif kind == "response_item":
            ptype = p.get("type")
            if ptype == "message" and p.get("role") in ("user", "developer", "system"):
                open_agent_step = False
                source = "user" if p["role"] == "user" else "system"
                steps.append(
                    {"source": source, "timestamp": ts, "message": _text_of(p.get("content"))}
                )
            elif ptype == "message":  # assistant
                step = agent_step(ts)
                step["message"] = (
                    step.get("message", "") + "\n" + _text_of(p.get("content"))
                ).strip()
                open_agent_step = False  # a message ends the model turn
            elif ptype == "reasoning":
                summary = "\n".join(s.get("text", "") for s in p.get("summary") or [])
                if summary:
                    pending_reasoning.append(summary)
            elif ptype in ("function_call", "custom_tool_call"):
                step = agent_step(ts)
                raw = p.get("arguments", p.get("input", ""))
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    args = {"raw": raw}
                step.setdefault("tool_calls", []).append(
                    {
                        "tool_call_id": p["call_id"],
                        "function_name": p["name"],
                        "arguments": args if isinstance(args, dict) else {"value": args},
                    }
                )
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                open_agent_step = False
                out = p.get("output")
                out = out.get("content", json.dumps(out)) if isinstance(out, dict) else out
                orphans += not _attach_result(steps, p["call_id"], _text_of(out))

    for step in steps:
        step.setdefault("message", "")
    steps = _renumber(steps)
    return {
        "schema_version": ATIF_VERSION,
        "session_id": meta.get("id") or session_ref,
        "agent": {
            "name": meta.get("originator") or "codex",
            "version": meta.get("cli_version") or "unknown",
            "model_name": model,
        },
        "steps": steps,
        "final_metrics": _final_metrics(steps),
        "extra": {"source_ref": session_ref, "adapter": "codex", "orphan_results": orphans},
    }


# --------------------------------------------------------------------------- pi


def pi_to_atif(rows: list[dict], session_ref: str) -> dict:
    """pi coding-agent session JSONL (version 3) -> ATIF.

    Each `message` row is one whole message: an assistant message is one model call (its
    `toolCall`s included), and each `toolResult` is attached to the call it answers. Rows are
    taken in file order; pi records branches with `parentId`, which these traces don't use.
    """
    steps: list[dict] = []
    session: dict = {}
    model = None
    orphans = 0

    for row in rows:
        kind, ts = row.get("type"), row.get("timestamp")
        if kind == "session":
            session = row
        elif kind == "model_change":
            model = row.get("modelId") or model
        elif kind == "message":
            msg = row.get("message") or {}
            role = msg.get("role")
            if role in ("developer", "system"):
                steps.append(
                    {"source": "system", "timestamp": ts, "message": _text_of(msg.get("content"))}
                )
            elif role == "user":
                text = PI_FILE_ATTACHMENT.sub(r"\1", _text_of(msg.get("content")))
                steps.append({"source": "user", "timestamp": ts, "message": text})
            elif role == "assistant":
                model = msg.get("model") or model
                step = {"source": "agent", "timestamp": ts, "model_name": model}
                for block in msg.get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        step["message"] = (step.get("message", "") + "\n" + block["text"]).strip()
                    elif block.get("type") == "thinking" and block.get("thinking"):
                        step["reasoning_content"] = (
                            step.get("reasoning_content", "") + "\n" + block["thinking"]
                        ).strip()
                    elif block.get("type") == "toolCall":
                        step.setdefault("tool_calls", []).append(
                            {
                                "tool_call_id": block["id"],
                                "function_name": block["name"],
                                "arguments": block.get("arguments") or {},
                            }
                        )
                if usage := msg.get("usage"):
                    step["metrics"] = {
                        "prompt_tokens": usage.get("input", 0) + usage.get("cacheRead", 0),
                        "completion_tokens": usage.get("output", 0),
                        "cached_tokens": usage.get("cacheRead", 0),
                    }
                steps.append(step)
            elif role == "toolResult":
                ok = _attach_result(
                    steps,
                    msg.get("toolCallId"),
                    _text_of(msg.get("content")),
                    bool(msg.get("isError")),
                )
                orphans += not ok

    for step in steps:
        step.setdefault("message", "")
    steps = _renumber(steps)
    return {
        "schema_version": ATIF_VERSION,
        "session_id": session.get("id") or session_ref,
        "agent": {
            "name": "pi",
            "version": str(session.get("version") or "unknown"),
            "model_name": model,
        },
        "steps": steps,
        "final_metrics": _final_metrics(steps),
        "extra": {
            "source_ref": session_ref,
            "adapter": "pi",
            "orphan_results": orphans,
            "cwd": session.get("cwd"),
        },
    }


ADAPTERS = {"claude-code": claude_code_to_atif, "codex": codex_to_atif, "pi": pi_to_atif}


def convert(path: str, fmt: str, session_ref: str) -> dict:
    rows, bad = [], 0
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    atif = ADAPTERS[fmt](rows, session_ref)
    atif["extra"]["unparsed_lines"] = bad
    return atif
