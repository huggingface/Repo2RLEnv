"""Record original API responses and token usage without changing their contents."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

LOCK = threading.Lock()


def record_response(response) -> None:
    destination = Path(os.environ["REPRO_API_RESPONSES"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.now(timezone.utc).isoformat(), "response": response.model_dump()}
    try:
        import litellm
        record["estimated_usd"] = litellm.completion_cost(completion_response=response)
        record["cost_source"] = "installed litellm rate table; estimate, not invoice"
    except Exception:
        record["estimated_usd"] = None
    with LOCK, destination.open("a") as stream:
        stream.write(json.dumps(record, default=str) + "\n")
