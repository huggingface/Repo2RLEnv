"""Private, bounded image-build diagnostics, including failed and timed-out builds."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from repo2rlenv.execution.lifecycle import save_record

MAX_STREAM_BYTES = 4 * 1024 * 1024
_ERROR_LINE = re.compile(
    r"^(?:Traceback \(most recent call last\):|[\w.]+(?:Error|Exception):|ERROR:|error:|fatal:)"
)


def build_excerpt(text: str, limit: int = 6000) -> str:
    """Keep causal lines even when Docker repeats a large inline RUN after them."""
    if len(text) <= limit:
        return text
    errors = []
    for line in text.splitlines():
        content = re.sub(r"^#\d+\s+(?:[\d.]+\s+)?", "", line).strip()
        if _ERROR_LINE.match(content):
            errors.append(line[: limit // 16])
            if len(errors) == 8:
                break
    return (
        text[: limit // 6]
        + "\n[omitted; selected error lines follow]\n"
        + "\n".join(errors)
        + "\n[omitted; final output follows]\n"
        + text[-limit // 6 :]
    )


def redact_build_text(value: str | bytes | None) -> str:
    """Remove known credentials before retaining or surfacing build diagnostics."""
    text = value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
    # Workers receive no model credentials. Still avoid retaining credentials
    # accidentally printed by a tool; never serialize the environment or argv.
    values = {
        value
        for name, value in os.environ.items()
        if len(value) >= 8 and re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", name, re.I)
    }
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1[REDACTED]@", text)
    return re.sub(r"(authorization:\s*(?:bearer|basic)\s+)\S+", r"\1[REDACTED]", text, flags=re.I)


def save_build_logs(prefix: Path, stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    """Save complete redacted streams up to the cap, explicitly mark larger logs."""
    records = {}
    summaries = []
    for name, raw in (("stdout", stdout), ("stderr", stderr)):
        text = redact_build_text(raw)
        data = text.encode()
        summary = build_excerpt(text)
        retained = data
        if len(data) > MAX_STREAM_BYTES:
            marker = (
                "\n[log exceeds private stream limit; omitted middle]\n" + summary + "\n"
            ).encode()
            side = (MAX_STREAM_BYTES - len(marker)) // 2
            retained = data[:side] + marker + data[-side:]
        path = prefix.with_suffix("." + name)
        path.write_bytes(retained)
        records[name] = {
            "path": path.name,
            "redacted_bytes": len(data),
            "retained_bytes": len(retained),
            "complete": len(data) <= MAX_STREAM_BYTES,
            "sha256": hashlib.sha256(retained).hexdigest(),
        }
        if summary:
            summaries.append(f"{name} ({path.name}):\n{summary}")
    save_record(prefix.with_suffix(".logs.json"), {"streams": records, "redacted": True})
    return "\n".join(summaries)
