"""Typed progress events for the terminal view and durable machine-readable logs."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProgressEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    recipe: str
    stage: str
    state: Literal["started", "progress", "completed", "failed", "cancelled"]
    task_id: str | None = None
    message: str = ""
    metrics: dict[str, int | float | str] = Field(default_factory=dict)


class EventJournal:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: ProgressEvent) -> None:
        data = event.model_dump_json() + "\n"
        with self._lock, self.path.open("a+b") as handle:
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    raise ValueError(
                        "Journal has an incomplete tail; reconcile it before appending"
                    )
            handle.write(data.encode("utf-8"))
            handle.flush()
            if event.state != "progress":
                os.fsync(handle.fileno())

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        # A process killed during append can leave a final partial record. Keep
        # that tail on disk for recovery; corruption of a complete line is an error.
        records = raw.split(b"\n")[:-1]
        return [ProgressEvent.model_validate(json.loads(line)).model_dump() for line in records]
