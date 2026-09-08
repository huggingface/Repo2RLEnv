"""Public-only instruction approval before expensive task construction.

``approve_instruction`` preserves every verdict and allows at most two shell-free
editor corrections. Operational failures require reconciliation; they are never
converted into semantic feedback or an approved specification.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.inference import inference_settings
from repo2rlenv.tasksmith import review, worker
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.models import safe_relative
from repo2rlenv.tasksmith.review import comprehension_review
from repo2rlenv.tasksmith.worker import artifact_stage, canonical_digest, save_json

POLICY_VERSION = 1
MAX_CORRECTIONS = 2
EDITOR_TURNS = 8


class SpecificationApprovalError(RuntimeError):
    """No approved specification is available; never an author-validator rejection."""


class PublicInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1)


def _inventory(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SpecificationApprovalError("Linked specification evidence")
        if path.is_file() and path.relative_to(root).as_posix() not in {
            "approval.json",
            "approval-operation.json",
        }:
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


async def approve_instruction(
    config: TasksmithConfig,
    budget: Budget,
    root: Path,
    deadline: float,
    initial_text: str,
    visible_files: list[str],
) -> dict:
    """Return approved ``instruction`` plus reviews, edits and hashed audit receipts.

    The caller supplies an actual complete base-file inventory and source-bound
    root. Neither private author artifacts nor task execution enter this phase.
    The final emitted instruction still needs its independent comprehension review.
    """
    if not isinstance(initial_text, str) or not initial_text.strip():
        raise SpecificationApprovalError("Public instruction must be nonempty")
    if not math.isfinite(deadline):
        raise SpecificationApprovalError("Invalid absolute specification deadline")
    try:
        if not isinstance(visible_files, list) or not visible_files:
            raise ValueError("Complete visible file inventory is required")
        for path in visible_files:
            safe_relative(path)
        if len(set(visible_files)) != len(visible_files):
            raise ValueError("Duplicate visible file paths")
    except (ValueError, TypeError) as exc:
        raise SpecificationApprovalError(str(exc)) from exc
    visible_files = sorted(visible_files)
    if len(initial_text) + len(json.dumps(visible_files)) > review.MAX_EVIDENCE_CHARACTERS:
        raise SpecificationApprovalError(
            "Complete public comprehension evidence exceeds review bound"
        )
    inputs = {
        "policy": POLICY_VERSION,
        "source_identity_root": str(root.resolve()),
        "config_digest": config.digest(),
        "budget": {
            "path": str(budget.path.resolve()),
            "scope": budget.scope,
            "limit": budget.limit,
            "scope_limit": budget.scope_limit,
            "group": budget.group,
            "group_limit": budget.group_limit,
        },
        "instruction": initial_text,
        "visible_files": visible_files,
        "inference": {
            "reviewer": inference_settings(config.reviewer_model),
            "editor": inference_settings(config.author_model),
        },
        "implementation": {
            name: hashlib.sha256(Path(module).read_bytes()).hexdigest()
            for name, module in {
                "specification": __file__,
                "review": review.__file__,
                "worker": worker.__file__,
            }.items()
        },
    }
    identity = canonical_digest(inputs)
    root.mkdir(parents=True, exist_ok=True)
    operation_path, result_path = root / "approval-operation.json", root / "approval.json"
    if operation_path.exists():
        operation = json.loads(operation_path.read_text())
        if operation.get("input_digest") != identity:
            raise SpecificationApprovalError("Specification root belongs to different inputs")
        if operation.get("status") != "completed":
            raise SpecificationApprovalError(
                "Retained specification requires reconciliation; no reroll"
            )
        if (
            not result_path.is_file()
            or result_path.is_symlink()
            or hashlib.sha256(result_path.read_bytes()).hexdigest()
            != operation.get("result_sha256")
        ):
            raise SpecificationApprovalError("Approved specification receipt changed")
        result = json.loads(result_path.read_text())
        if result["evidence_files"] != _inventory(root):
            raise SpecificationApprovalError("Retained specification evidence changed")
        return result
    if any(root.iterdir()):
        raise SpecificationApprovalError("Unclaimed specification evidence requires reconciliation")
    if deadline <= time.time():
        raise TimeoutError("Specification absolute deadline exhausted")
    operation = {"input_digest": identity, "status": "running", "deadline": deadline}
    with operation_path.open("x") as stream:
        json.dump(operation, stream)
        stream.flush()
        os.fsync(stream.fileno())
    save_json(root / "inputs.json", inputs)
    reviews, edits = [], []
    instruction = initial_text
    try:
        for correction in range(MAX_CORRECTIONS + 1):
            if deadline <= time.time():
                raise TimeoutError("Specification absolute deadline exhausted")
            public = {"instruction": instruction, "visible_files": visible_files}
            review_root = root / "reviews" / canonical_digest(public)
            verdict = await comprehension_review(
                config=config,
                budget=budget,
                root=review_root,
                deadline=deadline,
                instruction=instruction,
                visible_files=visible_files,
            )
            reviews.append(
                {
                    "instruction": instruction,
                    "root": str(review_root),
                    "review": verdict.model_dump(mode="json"),
                }
            )
            save_json(root / "review-history.json", reviews)
            if verdict.status == "pass":
                result = {
                    "input_digest": identity,
                    "instruction": instruction,
                    "reviews": reviews,
                    "edits": edits,
                    "evidence_files": _inventory(root),
                }
                save_json(result_path, result)
                operation.update(
                    status="completed",
                    result_sha256=hashlib.sha256(result_path.read_bytes()).hexdigest(),
                )
                save_json(operation_path, operation)
                return result
            if verdict.status != "fail":
                raise SpecificationApprovalError("Public comprehension is incomplete")
            if correction == MAX_CORRECTIONS:
                raise SpecificationApprovalError(
                    "No approved instruction after two public corrections"
                )
            editor_inputs = {
                **public,
                "required_feedback": [
                    row.model_dump(mode="json") for row in verdict.required_repairs
                ],
            }
            edit_root = root / "edits" / str(correction)
            edited = await artifact_stage(
                schema=PublicInstruction,
                stage="public-specification-editor",
                inputs=editor_inputs,
                system=(
                    "Rewrite only this public developer request to address the required review feedback. "
                    "Preserve its useful behavior and required public APIs. Replace implementation recipes "
                    "with observable requirements; do not invent missing facts or expand scope. You have "
                    "only public text and a file inventory, no reference implementation or private tests. "
                    "Return the complete instruction through submit_artifact; no shell is available."
                ),
                prompt=json.dumps(editor_inputs),
                root=edit_root,
                budget=budget,
                model=config.author_model,
                runtime="langgraph",
                max_cost=config.author_stage_limit_usd,
                max_turns=EDITOR_TURNS,
                deadline=deadline,
            )
            edits.append({"root": str(edit_root), "instruction": edited.instruction})
            save_json(root / "edit-history.json", edits)
            if not edited.instruction.strip() or edited.instruction == instruction:
                raise SpecificationApprovalError(
                    "Editor did not produce a changed public instruction"
                )
            instruction = edited.instruction
        raise AssertionError("Unreachable specification loop")
    except BaseException as exc:
        operation.update(status="incomplete", error=f"{type(exc).__name__}: {exc}")
        save_json(operation_path, operation)
        if isinstance(exc, ValueError):
            raise SpecificationApprovalError(str(exc)) from exc
        raise
