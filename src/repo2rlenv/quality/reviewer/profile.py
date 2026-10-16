"""Persist the exact reviewed excerpts and reviewer identity, outside the task."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

from repo2rlenv.execution.lifecycle import save_record

PROFILE = "evidence-review-v2"


def content_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def instructions() -> str:
    return files(__package__).joinpath("review.md").read_text()


def identity(options) -> dict:
    from repo2rlenv.quality.loop.client import prompt

    package = Path(__file__).parents[1]
    code = {
        str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in (package / "loop", package / "reviewer")
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.suffix in {".py", ".md"}
    }
    return {
        "profile": options.profile,
        "code": code,
        "review_prompt_sha256": content_hash(
            prompt("review") + ("\n" + instructions() if options.profile == PROFILE else "")
        ),
        "options": options.model_dump(mode="json"),
    }


def save_review(directory, key, task_hash, review, context, trials, reviewer):
    documents = dict(context.documents)
    evidence = {
        "documents": documents,
        "inventory": context.inventory,
        "trials": [t.model_dump(mode="json") for t in trials],
    }
    raw = review.model_dump(mode="json")
    review_id = content_hash(
        {"task": task_hash, "review": raw, "evidence": evidence, "reviewer": reviewer}
    )
    record = {
        "schema_version": "2",
        "profile": PROFILE,
        "review_id": review_id,
        "task_hash": task_hash,
        "reviewer_hash": content_hash(reviewer),
        "evidence_hash": content_hash(evidence),
        "review": raw,
        "findings": [
            dict(issue, issue_id=f"{review_id[:16]}-{i + 1}")
            for i, issue in enumerate(raw["issues"])
        ],
    }
    root = directory / "reviews" / key
    save_record(root / "evidence.json", evidence)
    save_record(root / "reviewer.json", reviewer)
    save_record(root / "findings.json", record)
    # One immutable journal per accepted review, rather than duplicating events on resume.
    save_record(
        root / "investigation.json",
        {"review_id": review_id, "investigations": raw["investigations"]},
    )
    save_record(
        directory / "findings.json",
        {"latest": str(root.relative_to(directory)), "review_id": review_id},
    )
    return record


def load_review(path: Path) -> tuple[dict, dict, dict]:
    """Validate persisted identities before feedback or benchmark scoring."""
    from repo2rlenv.quality.reviewer.models import EvidenceReview

    path = path.resolve()
    if path.is_file():
        path = path.parent
    pointer_id = None
    if (path / "findings.json").is_file():
        pointer = json.loads((path / "findings.json").read_text())
        if "latest" in pointer:
            pointer_id = pointer["review_id"]
            target = (path / pointer["latest"]).resolve()
            if not target.is_relative_to(path):
                raise ValueError("Review pointer escapes its directory")
            path = target
    record = json.loads((path / "findings.json").read_text())
    evidence = json.loads((path / "evidence.json").read_text())
    reviewer = json.loads((path / "reviewer.json").read_text())
    if record.get("schema_version") != "2" or record.get("profile") != PROFILE:
        raise ValueError("Expected an evidence-review-v2 findings record")
    parsed = EvidenceReview.model_validate(record["review"])
    expected = content_hash(
        {
            "task": record["task_hash"],
            "review": parsed.model_dump(mode="json"),
            "evidence": evidence,
            "reviewer": reviewer,
        }
    )
    if (
        (pointer_id is not None and pointer_id != expected)
        or record["review_id"] != expected
        or record["evidence_hash"] != content_hash(evidence)
        or record["reviewer_hash"] != content_hash(reviewer)
    ):
        raise ValueError("Review evidence or reviewer identity changed")
    findings = [
        dict(issue, issue_id=f"{expected[:16]}-{i + 1}")
        for i, issue in enumerate(record["review"]["issues"])
    ]
    if record["findings"] != findings:
        raise ValueError("Finding IDs or content changed")
    return record, evidence, reviewer
