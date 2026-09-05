"""Explicit, bounded autonomous repair of a retained conversion candidate."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Literal

from pydantic import Field

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import Budget, BudgetExceeded, register_scope_constraints
from repo2rlenv.curation.design import CandidateDesign
from repo2rlenv.curation.models import CampaignConfig, Review, StrictModel, validate_review_scores

MAX_FEEDBACK_BYTES = 64_000
SHA = r"^[0-9a-f]{64}$"


class RepairError(ValueError):
    """Invalid or conflicting repair provenance; no automatic fresh fallback."""


class BoundRepairFile(StrictModel):
    path: Path
    sha256: str = Field(pattern=SHA)


class RepairBudgetIdentity(StrictModel):
    scope: str = Field(min_length=1)
    scope_limit: float = Field(gt=0)
    group: str = Field(min_length=1)
    group_limit: float = Field(gt=0)


class RepairBudgetReceipt(StrictModel):
    ledger_path: Path
    global_limit: float = Field(gt=0)
    parent: RepairBudgetIdentity
    child: RepairBudgetIdentity
    parent_entries_sha256: str = Field(pattern=SHA)
    lineage_scopes: list[str] = Field(min_length=2)
    lineage_limit: float = Field(gt=0)
    phase_groups: list[str] = Field(min_length=1)
    phase_limit: float = Field(gt=0)


class SeedRepair(StrictModel):
    kind: Literal["assisted_autonomous_repair"] = "assisted_autonomous_repair"
    evidence_root: Path
    parent_root: Path
    parent_task_digest: str = Field(pattern=SHA)
    source: BoundRepairFile
    config: BoundRepairFile
    design: BoundRepairFile
    semantic_history: BoundRepairFile
    mechanical_history: BoundRepairFile
    author_traces: list[BoundRepairFile] = Field(min_length=1)
    # Required for a parent that was itself an explicit repair.
    revision_history: BoundRepairFile | None = None
    review: BoundRepairFile
    review_result: BoundRepairFile
    audit: BoundRepairFile
    budget_receipt: BoundRepairFile


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe(path: Path, root: Path, *, directory: bool = False) -> Path:
    path = path.absolute()
    if not path.is_relative_to(root) or ".." in path.parts:
        raise RepairError(f"Repair path outside evidence root: {path}")
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise RepairError(f"Symlink in repair path: {path}")
        if parent == root:
            break
    mode = path.stat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise RepairError(f"Nonregular repair evidence: {path}")
    return path


def _read(bound: BoundRepairFile, root: Path, maximum: int = 4_000_000) -> bytes:
    path = _safe(bound.path, root)
    if path.stat().st_size > maximum:
        raise RepairError(f"Oversized repair input: {path}")
    data = path.read_bytes()
    if _sha(data) != bound.sha256:
        raise RepairError(f"Repair input hash mismatch: {path}")
    return data


def _write(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def _lock(path: Path, *, blocking: bool = False):
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise RepairError("Repair lineage is already active") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _check_allocation(entries, receipt: RepairBudgetReceipt) -> None:
    lineage = sum(
        e["charged_usd"] for e in entries.values() if e.get("scope") in receipt.lineage_scopes
    )
    phase = sum(
        e["charged_usd"] for e in entries.values() if e.get("group") in receipt.phase_groups
    )
    if lineage + receipt.child.scope_limit > receipt.lineage_limit:
        raise BudgetExceeded("Repair lineage allowance exhausted")
    if phase + receipt.child.scope_limit > receipt.phase_limit:
        raise BudgetExceeded("Repair phase allowance exhausted")


class RepairSession:
    def __init__(self, root, context, design, feedback, used, budget, receipt):
        self.root, self.context, self.design = root, context, design
        self.feedback, self.used, self.budget = feedback, used, budget
        self.receipt = receipt

    def start_revision(self, revision: int) -> None:
        if revision != self.used:
            raise RepairError("Repair author revision counter mismatch")
        self.used += 1
        _write(self.root / "repair-progress.json", {"used_author_revisions": self.used})

    def restore_task(self, original: Path) -> Path:
        rows = json.loads((self.root / "submitted-drafts.json").read_text())
        latest = Path(rows[-1]["task"])
        if not latest.is_relative_to(self.root):
            return original
        _safe(latest, self.root, directory=True)
        for path in latest.rglob("*"):
            _safe(path, latest, directory=path.is_dir())
        if digest_task(latest) != rows[-1]["digest"]:
            raise RepairError("Last repair checkpoint digest changed")
        return latest

    def require_unreviewed_change(self, digest: str) -> None:
        if digest == self.context.parent_task_digest:
            raise ValueError(
                "The unchanged parent is not eligible for another review; repair the cited defects first"
            )
        for path in self.root.glob("revision-*/review.json"):
            _safe(path, self.root)
            evidence_path = _safe(path.parent / "evidence.json", self.root)
            if json.loads(evidence_path.read_text()).get("task_digest") == digest:
                try:
                    Review.model_validate_json(path.read_bytes())
                except ValueError:
                    continue
                raise ValueError(
                    "This repaired digest already has a final review; change the task rather than rerolling its judge"
                )

    def accounting(self) -> dict:
        with self.budget._locked() as state:
            entries = state["entries"]
            scopes = set(self.receipt.lineage_scopes)
            selected = {
                key: entry for key, entry in entries.items() if entry.get("scope") in scopes
            }
            return {
                "ledger_path": str(self.budget.path),
                "parent_scope": self.receipt.parent.scope,
                "repair_scope": self.budget.scope,
                "lineage_scopes": sorted(scopes),
                "lineage_entry_ids": sorted(selected),
                "lineage_charged_or_reserved_usd": sum(e["charged_usd"] for e in selected.values()),
                "repair_charged_or_reserved_usd": sum(
                    e["charged_usd"]
                    for e in selected.values()
                    if e.get("scope") == self.budget.scope
                ),
            }


def _history(data: bytes, *, semantic: bool) -> list[dict]:
    rows = json.loads(data)
    if not isinstance(rows, list):
        raise RepairError("Repair submission history must be an array")
    for row in rows:
        keys = {"digest", "task"} if semantic else {"task", "reason"}
        if (
            not isinstance(row, dict)
            or set(row) != keys
            or not all(isinstance(v, str) for v in row.values())
        ):
            raise RepairError("Invalid repair submission history")
        if semantic and not re.fullmatch(SHA, row["digest"]):
            raise RepairError("Invalid semantic history digest")
    if semantic and len({row["digest"] for row in rows}) != len(rows):
        raise RepairError("Duplicate semantic history digest")
    return rows


@contextmanager
def prepare_seed_repair(
    context: SeedRepair,
    seed_task: Path,
    root: Path,
    source: dict,
    config: CampaignConfig,
    budget: Budget,
):
    """Validate, claim one child, inherit allowances, and lock its whole author run."""
    if not isinstance(context, SeedRepair) or config.submission_policy != "conversion":
        raise RepairError("Seed repair requires typed context and conversion policy")
    evidence_root = context.evidence_root.absolute()
    _safe(evidence_root, evidence_root, directory=True)
    parent = _safe(context.parent_root, evidence_root, directory=True)
    task = _safe(seed_task, parent, directory=True)
    total = 0
    for path in task.rglob("*"):
        _safe(path, task, directory=path.is_dir())
        if path.is_file():
            total += path.stat().st_size
    if total > 32_000_000 or digest_task(task) != context.parent_task_digest:
        raise RepairError("Parent task digest mismatch or oversized task")
    root = root.absolute()
    if root.is_relative_to(parent) or parent.is_relative_to(root):
        raise RepairError("Repair child must be separate from its parent")
    # Resolve every existing component, even when the fresh root is absent.
    for part in [root, *root.parents]:
        if part.is_symlink():
            raise RepairError("Repair output cannot traverse symlinks")
    if not root.is_relative_to(evidence_root):
        raise RepairError("Repair output outside evidence root")
    raw = {
        name: _read(getattr(context, name), evidence_root)
        for name in (
            "source",
            "config",
            "design",
            "semantic_history",
            "mechanical_history",
            "review",
            "review_result",
            "audit",
            "budget_receipt",
        )
    }
    for name, path in (
        ("source", parent / "source.json"),
        ("design", parent / "design.json"),
        ("semantic_history", parent / "submitted-drafts.json"),
        ("mechanical_history", parent / "mechanical-submissions.json"),
    ):
        if getattr(context, name).path.absolute() != path:
            raise RepairError(f"Repair {name} does not belong to parent")
    if (
        json.loads(raw["source"]) != source
        or CampaignConfig.model_validate_json(raw["config"]) != config
    ):
        raise RepairError("Repair source/config mismatch")
    stored = json.loads(raw["design"])
    if stored.get("source_digest") != _sha(_canonical(source)):
        raise RepairError("Repair design source mismatch")
    design = CandidateDesign.model_validate(stored["design"])
    semantics = _history(raw["semantic_history"], semantic=True)
    mechanics = _history(raw["mechanical_history"], semantic=False)
    if context.parent_task_digest not in {row["digest"] for row in semantics}:
        raise RepairError("Parent task absent from semantic history")
    if (
        len(semantics) >= config.max_candidate_drafts
        or len(mechanics) >= config.max_mechanical_submissions
    ):
        raise RepairError("Inherited repair submission allowance exhausted")
    indices = []
    for bound in context.author_traces:
        path = _safe(bound.path, parent)
        match = re.fullmatch(r"author-(\d+)\.jsonl", path.name)
        if path.parent != parent or not match:
            raise RepairError("Invalid parent author trace path")
        _read(bound, parent)
        indices.append(int(match[1]))
    if {b.path.absolute() for b in context.author_traces} != set(
        parent.glob("author-*.jsonl")
    ) or len(set(indices)) != len(indices):
        raise RepairError("Incomplete parent author history")
    if context.revision_history is None:
        if (parent / "repair-progress.json").exists() or sorted(indices) != list(
            range(len(indices))
        ):
            raise RepairError("Missing parent repair revision history")
        used = len(indices)
    else:
        if context.revision_history.path.absolute() != parent / "repair-progress.json":
            raise RepairError("Invalid parent revision history path")
        used = json.loads(_read(context.revision_history, parent))["used_author_revisions"]
        if type(used) is not int or used <= max(indices):
            raise RepairError("Invalid parent revision count")
    if used >= config.max_revisions:
        raise RepairError("Inherited author revision allowance exhausted")
    review = Review.model_validate_json(raw["review"])
    result, audit = json.loads(raw["review_result"]), json.loads(raw["audit"])
    if (
        result.get("task_digest") != context.parent_task_digest
        or audit.get("task_digest") != context.parent_task_digest
    ):
        raise RepairError("Review/audit task digest mismatch")
    if "review" in result and Review.model_validate(result["review"]) != review:
        raise RepairError("Parent judge review and result disagree")
    validate_review_scores(result, review, config)
    feedback = (
        "\nExplicitly assisted autonomous repair of a retained task, not fresh conversion yield. "
        "Preserve the complete public behavior; investigate the cited evidence and repair only "
        "supported defects. The old task and verdict remain immutable. Audit suggestions are "
        "separate from the judge verdict and are not proof of an executed counterexample.\n"
        "Historical judge review (not a new admission receipt):\n"
        + raw["review"].decode()
        + "\nHistorical automatic result:\n"
        + json.dumps({k: v for k, v in result.items() if k != "review"})
        + "\nIndependent audit suggestions, bound to the parent task:\n"
        + raw["audit"].decode()
    )
    if len(feedback.encode()) > MAX_FEEDBACK_BYTES:
        raise RepairError("Oversized repair feedback; no silent truncation")
    receipt = RepairBudgetReceipt.model_validate_json(raw["budget_receipt"])
    identity = receipt.child
    if (
        (
            receipt.ledger_path.absolute(),
            receipt.global_limit,
            identity.scope,
            identity.scope_limit,
            identity.group,
            identity.group_limit,
        )
        != (
            budget.path.absolute(),
            budget.limit,
            budget.scope,
            budget.scope_limit,
            budget.group,
            budget.group_limit,
        )
        or receipt.parent.scope == identity.scope
        or receipt.parent.group == identity.group
    ):
        raise RepairError("Repair budget identity mismatch; require an explicit new scope/group")
    if not {receipt.parent.group, identity.group}.issubset(receipt.phase_groups):
        raise RepairError("Repair phase excludes parent or child")
    if not {receipt.parent.scope, identity.scope}.issubset(receipt.lineage_scopes):
        raise RepairError("Repair lineage excludes parent or child")
    if context.revision_history is not None:
        prior_path = _safe(parent / "repair-input.json", parent)
        prior_input = json.loads(prior_path.read_text())
        prior_context = SeedRepair.model_validate(prior_input["context"])
        prior_claim = _safe(prior_context.parent_root / "repair-child.json", evidence_root)
        if json.loads(prior_claim.read_text()) != {
            "root": str(parent),
            "input_sha256": _sha(_canonical(prior_input)),
        }:
            raise RepairError("Parent repair input no longer matches its lineage claim")
        prior_budget = RepairBudgetReceipt.model_validate_json(
            _read(prior_context.budget_receipt, evidence_root)
        )
        if (
            receipt.parent != prior_budget.child
            or not set(prior_budget.lineage_scopes).issubset(receipt.lineage_scopes)
            or not set(prior_budget.phase_groups).issubset(receipt.phase_groups)
        ):
            raise RepairError("Repair receipt drops ancestor budget identity")
    _safe(budget.path, evidence_root)
    payload = {
        "context": context.model_dump(mode="json"),
        "feedback": feedback,
        "inherited_author_revisions": used,
        "classification": context.kind,
    }
    payload_hash = _sha(_canonical(payload))
    claim_path = parent / "repair-child.json"
    # Parent execution lock prevents a repair-of-repair from reading active state.
    parent_lock = (
        _lock(parent / ".repair-execution.lock")
        if context.revision_history is not None
        else nullcontext()
    )
    with parent_lock:
        with _lock(budget.path.with_suffix(".lock"), blocking=True):
            ledger = json.loads(budget.path.read_text())
            entries = ledger["entries"]
            retained = {k: v for k, v in entries.items() if v.get("scope") == receipt.parent.scope}
            if not retained or _sha(_canonical(retained)) != receipt.parent_entries_sha256:
                raise RepairError("Retained parent ledger entries changed or missing")
            if any(entry.get("group") != receipt.parent.group for entry in retained.values()):
                raise RepairError("Retained parent ledger group mismatch")
            constraints = {
                "repair_lineage": {
                    "scopes": receipt.lineage_scopes,
                    "groups": [],
                    "limit_usd": receipt.lineage_limit,
                },
                "repair_phase": {
                    "scopes": [],
                    "groups": receipt.phase_groups,
                    "limit_usd": receipt.phase_limit,
                },
            }
            if identity.scope not in ledger.get("scope_constraints", {}) and any(
                e.get("scope") == identity.scope for e in entries.values()
            ):
                raise RepairError("Charged repair scope lacks its ledger constraints")
            register_scope_constraints(ledger, identity.scope, constraints)
            claim = {"root": str(root), "input_sha256": payload_hash}
            lineage_key = _sha(
                _canonical(
                    {
                        "parent_scope": receipt.parent.scope,
                        "task_digest": context.parent_task_digest,
                        "source_digest": _sha(_canonical(source)),
                    }
                )
            )
            ledger_claim = {**claim, "parent_root": str(parent), "child_scope": identity.scope}
            claimed = ledger.setdefault("repair_children", {}).get(lineage_key)
            if claimed is not None and claimed != ledger_claim:
                raise RepairError(
                    "Parent already claimed in the shared ledger by a different repair child"
                )
            if claim_path.exists():
                _safe(claim_path, parent)
                if json.loads(claim_path.read_text()) != claim:
                    raise RepairError("Parent already claimed by a different repair child")
            else:
                if root.exists() and any(root.iterdir()):
                    raise RepairError("Repair output already contains unrelated state")
                child = sum(
                    e["charged_usd"] for e in entries.values() if e.get("scope") == identity.scope
                )
                if child or any(e.get("group") == identity.group for e in entries.values()):
                    raise RepairError("New repair scope already has charges")
                _check_allocation(entries, receipt)
                _write(claim_path, claim)
            ledger["repair_children"][lineage_key] = ledger_claim
            _write(budget.path, ledger)
        root.mkdir(parents=True, exist_ok=True)
        with _lock(root / ".repair-execution.lock"):
            if (root / "repair-result.json").exists():
                raise RepairError("Repair already has a terminal result; no automatic reroll")
            verdict_path = root / "verdict.json"
            if verdict_path.exists():
                _safe(verdict_path, root)
                if json.loads(verdict_path.read_text()).get("status") == "accepted":
                    raise RepairError(
                        "Accepted repair verdict requires admission recovery, not another author"
                    )
            inputs = root / "repair-input.json"
            if inputs.exists():
                _safe(inputs, root)
                if json.loads(inputs.read_text()) != payload:
                    raise RepairError("Retained repair input changed")
                for name, inherited in (
                    ("submitted-drafts.json", semantics),
                    ("mechanical-submissions.json", mechanics),
                ):
                    path = _safe(root / name, root)
                    rows = _history(path.read_bytes(), semantic=name.startswith("submitted"))
                    if rows[: len(inherited)] != inherited:
                        raise RepairError("Inherited child submission history changed")
                    limit = (
                        config.max_candidate_drafts
                        if name.startswith("submitted")
                        else config.max_mechanical_submissions
                    )
                    if len(rows) >= limit:
                        raise RepairError("Repair submission allowance exhausted")
                progress = _safe(root / "repair-progress.json", root)
                current = json.loads(progress.read_text())["used_author_revisions"]
                if type(current) is not int or not used <= current < config.max_revisions:
                    raise RepairError("Repair author revision allowance exhausted or changed")
                used = current
            else:
                if any(root.glob("author-*.jsonl")) or (root / "repair-progress.json").exists():
                    raise RepairError("Incomplete repair checkpoint; no fresh fallback")
                _write(root / "submitted-drafts.json", semantics)
                _write(root / "mechanical-submissions.json", mechanics)
                _write(root / "design.json", stored)
                _write(root / "repair-progress.json", {"used_author_revisions": used})
                _write(inputs, payload)
            yield RepairSession(root, context, design, feedback, used, budget, receipt)
