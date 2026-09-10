"""Write compact evidence without emitting full traces or credentials."""

from __future__ import annotations

import json
from pathlib import Path

summary = {"api_usage": {}, "native_tasks": {}, "swe_smith": {}}
for p in Path("/evidence").glob("*/*api.jsonl"):
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    summary["api_usage"][str(p)] = {
        "requests": len(rows),
        "estimated_usd": sum(r.get("estimated_usd") or 0 for r in rows),
        "unpriced_requests": sum(r.get("estimated_usd") is None for r in rows),
        "models": sorted({r["response"].get("model", "unknown") for r in rows}),
        "finish_reasons": [
            r["response"].get("choices", [{}])[0].get("finish_reason") for r in rows
        ],
        "usage": [r["response"].get("usage") for r in rows],
    }
for project in ["endless", "tmax"]:
    for p in Path("/work", project).glob("native-*"):
        summary["native_tasks"][str(p)] = [str(x) for x in sorted(p.glob("task_*"))]
for p in Path("/work/swesmith").glob("validated*.json"):
    data = json.loads(p.read_text())
    summary["swe_smith"][str(p)] = {
        "instances": len(data),
        "descriptions_present": sum(bool(x.get("problem_statement")) for x in data),
    }
Path("/evidence/summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
