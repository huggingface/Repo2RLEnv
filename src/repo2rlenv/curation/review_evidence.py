"""Deterministic, lossless action evidence and required-read coverage for final review."""

from __future__ import annotations

import difflib
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from repo2rlenv.curation.inference import inference_settings

POLICY = "complete-actions-v1"
MAX_RAW_TRACE_BYTES = 4_000_000
MAX_PROJECTION_CHARS = 256_000
MAX_REQUIRED_CHARS = 512_000


class ReviewEvidenceError(ValueError):
    """Required final-review evidence is unavailable or incomplete; not a task verdict."""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def policy_identity(model: str, acceptance_policy: str) -> dict:
    """Separate judge-evidence policy identity; solver inference identities do not change."""
    return {
        "policy": POLICY,
        "projection_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "inference": inference_settings(model),
        "acceptance_policy": acceptance_policy,
        "limits": {
            "raw_trace_bytes": MAX_RAW_TRACE_BYTES,
            "projection_chars": MAX_PROJECTION_CHARS,
            "required_chars": MAX_REQUIRED_CHARS,
        },
    }


def _thinking(blocks: list, removed: list, path: str) -> None:
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "thinking" and "signature" in block:
            del block["signature"]
            removed.append(f"{path}/{i}/signature")
        elif block.get("type") == "redacted_thinking" and "data" in block:
            del block["data"]
            removed.append(f"{path}/{i}/data (opaque redacted thinking)")


def project_trace(raw: bytes, label: str) -> tuple[str, dict]:
    if not raw or len(raw) > MAX_RAW_TRACE_BYTES or not raw.endswith(b"\n"):
        raise ReviewEvidenceError(f"{label}: missing, oversized or torn action trace")
    try:
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise ReviewEvidenceError(f"{label}: malformed action trace") from exc
    if not events or any(not isinstance(event, dict) for event in events):
        raise ReviewEvidenceError(f"{label}: unsupported action trace")
    if not any(
        event.get("kind") == "model" and isinstance(event.get("message"), dict) for event in events
    ):
        raise ReviewEvidenceError(f"{label}: trace has no model action evidence")
    projected, removed = [], []
    for i, original in enumerate(events):
        event = deepcopy(original)
        message = event.get("message")
        if isinstance(message, dict):
            for container, prefix in (
                (message, "message"),
                (message.get("provider_specific_fields"), "message/provider_specific_fields"),
            ):
                if not isinstance(container, dict):
                    continue
                for field in ("thinking_blocks", "content"):
                    if isinstance(container.get(field), list):
                        _thinking(container[field], removed, f"event/{i}/{prefix}/{field}")
            provider = message.get("provider_specific_fields")
            if (
                isinstance(provider, dict)
                and "thinking_blocks" in message
                and "thinking_blocks" in provider
                and provider.get("thinking_blocks") == message["thinking_blocks"]
            ):
                del provider["thinking_blocks"]
                removed.append(
                    f"event/{i}/message/provider_specific_fields/thinking_blocks (exact duplicate)"
                )
        # Keep every unknown field/event, all readable reasoning, content, commands,
        # arguments and outputs. Never recursively strip keys in untrusted tool data.
        projected.append(event)
    text = (
        f"Action trace for {label}. Only opaque metadata/exact duplicates removed.\n"
        + "\n".join(json.dumps(event, ensure_ascii=False, indent=2) for event in projected)
        + "\n"
    )
    if len(text) > MAX_PROJECTION_CHARS:
        raise ReviewEvidenceError(f"{label}: full action projection exceeds review bound")
    return text, {
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "projection_sha256": sha(text),
        "events": len(events),
        "removed_metadata": removed,
    }


def submission_diff(
    label: str, changes: list[dict], texts: dict[str, str], *, complete: bool
) -> str:
    if not complete:
        raise ReviewEvidenceError(
            f"{label}: incomplete baseline/submission inventory; cannot prove changes"
        )
    output = [f"Submitted changes for {label}; generated from protected export snapshots.\n"]
    changed = [row for row in changes if row.get("trial") == label and "submission" in row]
    for row in changed:
        status, before_name, after_name = row["status"], row.get("baseline"), row.get("evidence")
        if status not in {"modified", "added", "deleted"}:
            raise ReviewEvidenceError(f"{label}: unavailable baseline for {row['submission']}")
        if (status != "added" and before_name not in texts) or (
            status != "deleted" and after_name not in texts
        ):
            raise ReviewEvidenceError(f"{label}: incomplete changed text for {row['submission']}")
        before = "" if status == "added" else texts[before_name]
        after = "" if status == "deleted" else texts[after_name]
        output.append(
            json.dumps(
                {
                    "submission": row["submission"],
                    "status": status,
                    "baseline": before_name,
                    "submitted": after_name,
                    "before_sha256": sha(before),
                    "after_sha256": sha(after),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        for line in difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=before_name or "/dev/null",
            tofile=after_name or "/dev/null",
            n=3,
        ):
            output.append(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            )
    if not changed:
        output.append(
            "No submitted source changes; complete export inventories and hashes agree.\n"
        )
    text = "".join(output)
    if len(text) > MAX_PROJECTION_CHARS:
        raise ReviewEvidenceError(f"{label}: full submission diff exceeds review bound")
    return text


class RequiredReads:
    def __init__(self, texts: dict[str, str]):
        if (
            not texts
            or sum(map(len, texts.values())) > MAX_REQUIRED_CHARS
            or any(not isinstance(text, str) for text in texts.values())
        ):
            raise ReviewEvidenceError(
                "Required final-review evidence is empty or exceeds its bound"
            )
        self.texts = dict(texts)
        self.reads: dict[str, list[list[int]]] = {name: [] for name in texts}

    def observe(self, path: str, start: int, end: int) -> None:
        if path in self.texts:
            self.reads[path].append([start, end])

    def missing(self) -> dict[str, list[list[int]]]:
        missing = {}
        for path, text in self.texts.items():
            cursor, gaps = 0, []
            for start, end in sorted(self.reads[path]):
                if start > cursor:
                    gaps.append([cursor, start])
                cursor = max(cursor, end)
            if cursor < len(text):
                gaps.append([cursor, len(text)])
            if gaps:
                missing[path] = gaps
        return missing

    def feedback(self, _content: str = "") -> str | None:
        missing = self.missing()
        if not missing:
            return None
        entries = [f"{name}: {spans[:3]}" for name, spans in list(missing.items())[:8]]
        return (
            "Final review cannot finish until required evidence is read completely. "
            "Read the missing character ranges below; batch/paginate within the existing turn limit. "
            "Do not change the verdict merely to pass this read check.\n"
            + "\n".join(entries)
            + f"\n{len(missing)} required files remain incomplete."
        )

    def receipt(self) -> dict:
        return {
            "required_sha256": {name: sha(text) for name, text in self.texts.items()},
            "reads": self.reads,
            "missing": self.missing(),
            "complete": not self.missing(),
        }


def observed_required_reads(texts: dict[str, str], messages: list[dict]) -> RequiredReads:
    """Reconstruct delivered required pages from retained assistant/tool message pairs."""
    observed = RequiredReads(texts)
    pending, seen = {}, set()
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                identity = call["id"]
                if identity in seen:
                    raise ReviewEvidenceError("Duplicate retained evidence-read call")
                seen.add(identity)
                pending[identity] = call
        elif message.get("role") == "tool":
            identity = message.get("tool_call_id")
            if identity not in pending:
                raise ReviewEvidenceError("Unmatched retained evidence output")
            call = pending.pop(identity)
            if call["function"]["name"] != "read_evidence":
                continue
            if isinstance(message.get("content"), str) and message["content"].startswith(
                "Tool input error: "
            ):
                continue  # A corrected invalid call earns no read credit, but is not a verdict defect.
            arguments = json.loads(call["function"]["arguments"])
            path = arguments.get("path")
            if path not in texts:
                continue
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 12000)
            if type(offset) is not int or type(limit) is not int:
                raise ReviewEvidenceError("Invalid retained required-read interval")
            text = texts[path]
            start, size = max(0, offset), min(max(1, limit), 16000)
            end = min(start + size, len(text))
            page = f"{path}: characters {start}:{end} of {len(text)}\n" + text[start:end]
            output = message.get("content")
            if (
                not isinstance(output, str)
                or not output.startswith(page)
                or (
                    output[len(page) :] and not output[len(page) :].startswith("\nRead progress:\n")
                )
            ):
                raise ReviewEvidenceError("Required read output differs from retained evidence")
            if len(output) >= 24000:
                raise ReviewEvidenceError("Required read may have been truncated")
            observed.observe(path, min(start, len(text)), end)
    if pending:
        raise ReviewEvidenceError("Retained review has uncompleted tool calls")
    return observed
