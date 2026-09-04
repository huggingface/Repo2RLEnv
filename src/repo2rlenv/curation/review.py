from __future__ import annotations

import json
from pathlib import Path

from repo2rlenv.curation.agent import run_agent
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import Review, TrialEvidence
from repo2rlenv.curation.prompts import JUDGE


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


async def review(
    task: Path, root: Path, trials: list[TrialEvidence], *, model: str, budget: Budget
) -> Review:
    root = root.resolve()
    files = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and (
            p.name == "Dockerfile"
            or p.suffix in {".md", ".py", ".sh", ".json", ".jsonl", ".toml", ".txt"}
        )
        and not any(part in {"artifacts", ".git"} for part in p.relative_to(root).parts)
    ]
    catalog = "\n".join(f"{p.relative_to(root)} ({p.stat().st_size} bytes)" for p in files)
    allowed = {p.resolve() for p in files}

    async def read_evidence(path: str, offset: int = 0, limit: int = 12000) -> str:
        target = (root / path).resolve()
        if target not in allowed:
            raise ValueError("Path is not a listed evidence file")
        text = target.read_text(errors="replace")
        offset, limit = max(0, offset), min(max(1, limit), 22000)
        # Plain text avoids JSON escaping expanding pages past the agent tool
        # limit, which previously removed evidence from the middle of a page.
        return (
            f"{path}: characters {offset}:{min(offset + limit, len(text))} "
            f"of {len(text)}\n" + text[offset : offset + limit]
        )

    tool = {
        "type": "function",
        "function": {
            "name": "read_evidence",
            "description": "Read task or complete trajectory evidence; paginate by character offset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    }
    prompt = (
        "Task: "
        + str(task.relative_to(root))
        + "\nEvidence catalog:\n"
        + catalog
        + "\nTrial results:\n"
        + json.dumps([t.model_dump() for t in trials])
        + "\nRead the instruction, contract, tests, oracle and actual solver/adversary traces. "
        "Inspect verifier output for failures. Cite evidence paths and specific events. "
        "Return the complete structured review when done.\nSchema:\n"
        + json.dumps(Review.model_json_schema())
    )
    state = await run_agent(
        model=model,
        system=JUDGE,
        prompt=prompt,
        budget=budget,
        tools=[tool],
        handlers={"read_evidence": read_evidence},
        trace=root / "judge-trace.jsonl",
        max_turns=16,
        max_cost=8,
    )
    final = state["messages"][-1].get("content") or ""
    result = Review.model_validate(parse_json(final))
    (root / "review.json").write_text(result.model_dump_json(indent=2))
    return result
