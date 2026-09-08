"""Review-only recovery from complete, immutable trial evidence.

Preparation opens SQLite read-only. Execution owns a separate journal operation;
it never retries trials, changes the parent validation, or extends its budget/deadline.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import BudgetExceeded
from repo2rlenv.curation.evaluate import TAMPER, inspect_execution
from repo2rlenv.curation.inference import inference_digest
from repo2rlenv.tasksmith.authoring import Construction
from repo2rlenv.tasksmith.contracts import bind_contracts
from repo2rlenv.tasksmith.models import (
    AdmissionContext,
    ArtifactRef,
    EvidenceRef,
    OperationKey,
    OperationRecord,
    PRIdentity,
    QualityReport,
    TrialRecord,
    TrialRequirement,
    admission_reasons,
)
from repo2rlenv.tasksmith.providers import BuildSpec
from repo2rlenv.tasksmith.review import (
    FINAL_REQUIRED_EVIDENCE,
    ComprehensionReview,
    VerifierCritique,
)
from repo2rlenv.tasksmith.state import ReconciliationRequired
from repo2rlenv.tasksmith.trials import (
    TrialOutcome,
    _cleanup_receipts,
    _inspect_collection,
    _resources,
    _task_policy,
)
from repo2rlenv.tasksmith.validation import _trial_text, paired_witness, submission_changes
from repo2rlenv.tasksmith.worker import canonical_digest

POLICY_VERSION = 1


class FinalizationError(ValueError):
    """Retained evidence cannot authorize a fresh review or admission."""


def _read(path: Path, hashes: dict | None = None, limit=64 * 1024 * 1024) -> bytes:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise FinalizationError(f"Linked retained evidence: {path}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise FinalizationError(f"Nonregular or oversized evidence: {path}")
    raw = path.read_bytes()
    if len(raw) != info.st_size:
        raise FinalizationError("Evidence changed while reading")
    if hashes is not None:
        hashes[str(path.resolve())] = hashlib.sha256(raw).hexdigest()
    return raw


def _json(path, hashes=None):
    return json.loads(
        _read(path, hashes), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x))
    )


def _blob(journal_path, ref, hashes):
    expected = f"objects/{ref.sha256[:2]}/{ref.sha256}"
    if ref.path != expected:
        raise FinalizationError("Journal artifact path differs from content identity")
    raw = _read(journal_path.parent / (journal_path.stem + "-artifacts") / expected, hashes)
    if len(raw) != ref.size_bytes or hashlib.sha256(raw).hexdigest() != ref.sha256:
        raise FinalizationError("Journal artifact hash/size changed")
    return raw


def _snapshot(journal_path, pr_id, parent_id):
    if any(p.is_symlink() for p in (journal_path, *journal_path.parents)):
        raise FinalizationError("Linked journal path")
    with sqlite3.connect(journal_path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        pr = connection.execute("SELECT * FROM prs WHERE pr_id=?", (pr_id,)).fetchone()
        if pr is None:
            raise FinalizationError("Unknown frozen PR")
        operations = [
            OperationRecord.model_validate_json(row[0])
            for row in connection.execute("SELECT record FROM operations WHERE pr_id=?", (pr_id,))
        ]
        parents = [op for op in operations if op.key.operation_id == parent_id]
        if len(parents) != 1:
            raise FinalizationError("Unknown parent validation operation")
        artifacts = {}
        for row in connection.execute("SELECT * FROM artifacts WHERE operation_id=?", (parent_id,)):
            if row["status"] != "committed":
                raise FinalizationError("Parent has an incomplete artifact commit")
            artifacts[row["name"]] = ArtifactRef.model_validate_json(row["reference"])
        revisions = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM revisions WHERE pr_id=? ORDER BY revision", (pr_id,)
            )
        ]
        selected = connection.execute("SELECT * FROM selections WHERE pr_id=?", (pr_id,)).fetchone()
        return (
            dict(pr),
            parents[0],
            operations,
            artifacts,
            revisions,
            dict(selected) if selected else None,
        )


def _requirements(value, critic, config):
    rows = [
        ("baseline", "baseline", 0, "true", False, None),
        ("oracle-0", "oracle", 1, None, True, None),
        ("oracle-1", "oracle", 1, None, True, None),
    ]
    for kind, prefix, controls, reward in (
        ("negative_control", "negative", value.contract.mutations, 0),
        ("positive_control", "positive", value.contract.equivalents, 1),
    ):
        rows.extend(
            (f"{prefix}-{i}", kind, reward, c.script, True, None) for i, c in enumerate(controls)
        )
    rows += [
        ("tamper", "tamper", 0, TAMPER, False, None),
        ("challenge", "independent_challenge", 0, critic.challenge.script, True, None),
    ]
    rows += [
        (f"solver-{i}", "solver", None, None, False, model)
        for i, model in enumerate(config.solver_models)
    ]
    rows += [("adversary", "adversary", None, None, False, config.solver_models[0])]
    return rows


def _texts(task, construction, bundle, outcomes, witness, comprehension, critic):
    """Reconstruct the existing validation dossier without executing any artifact."""
    value = Construction.model_validate(construction["artifact"])
    return {
        "instruction": (task / "instruction.md").read_text(),
        "contract": bundle.model_dump_json(indent=2),
        "protected_tests": (task / "tests/test_contract.py").read_text()
        + "\nSOURCE OBSERVATION PROBE\n"
        + value.source_origin_probe,
        "oracle": (task / "solution/solve.sh").read_text()
        + "\n"
        + _trial_text(outcomes["oracle-0"])
        + "\n"
        + _trial_text(outcomes["oracle-1"]),
        "environment": "\n".join(
            (task / p).read_text()
            for p in (
                "task.toml",
                "environment/Dockerfile",
                "tests/Dockerfile",
                "tests/runner.py",
                "tests/probe.py",
            )
        ),
        "controls": "\n".join(
            _trial_text(v)
            for k, v in outcomes.items()
            if k not in {"oracle-0", "oracle-1", "solver-0", "solver-1", "adversary"}
        )
        + "\n"
        + value.contract.model_dump_json(indent=2),
        "solver_0": _trial_text(outcomes["solver-0"], trace=True),
        "solver_1": _trial_text(outcomes["solver-1"], trace=True),
        "adversary": _trial_text(outcomes["adversary"], trace=True),
        "submissions": submission_changes(outcomes["baseline"], outcomes),
        "bootstrap": json.dumps(construction["readiness"]),
        "comprehension": comprehension.model_dump_json(indent=2),
        "critic": critic.model_dump_json(indent=2),
        "source_witness": json.dumps(witness, indent=2),
    }


@dataclass(frozen=True)
class PreparedFinalization:
    journal_path: Path
    parent_operation_id: str
    validation_root: Path
    source: dict
    task_path: Path
    deadline: float
    prior_review_charge_usd: float
    evidence: dict[str, str]
    context: AdmissionContext
    proof: dict

    @property
    def digest(self):
        return canonical_digest(self.proof)


def _review_delivery(root, report, prepared, config, policy):
    """Bind the complete callback transport, including exact prompt and response bytes."""
    path = root / "operation.json"
    if not path.exists():
        if policy.get("delivery_policy"):
            raise FinalizationError("Required complete review delivery receipt is missing")
        return None
    operation = _json(path)
    names = {"inputs.json", "response.json", "delivery.json", "quality-report.json"}
    if operation.get("status") != "completed" or set(operation.get("files", {})) != names:
        raise FinalizationError("Final review delivery is incomplete")
    raw = {name: _read(root / name) for name in names}
    if any(hashlib.sha256(raw[name]).hexdigest() != operation["files"][name] for name in names):
        raise FinalizationError("Final review delivery file hash changed")
    inputs = json.loads(raw["inputs.json"])
    delivery = json.loads(raw["delivery.json"])
    if (
        inputs.get("config") != config.model_dump(mode="json")
        or inputs.get("deadline") != prepared.deadline
        or inputs.get("prior_review_charge_usd") != prepared.prior_review_charge_usd
        or inputs.get("root") != str(root)
        or (policy.get("delivery_policy") and inputs.get("policy") != policy["delivery_policy"])
        or operation.get("input_digest") != canonical_digest(inputs)
        or delivery.get("input_digest") != operation["input_digest"]
        or QualityReport.model_validate_json(raw["quality-report.json"]) != report
    ):
        raise FinalizationError("Final review delivery identity or report differs")
    return {
        "root": str(root),
        "operation": _read(path).decode(),
        "files": {
            name: {"sha256": operation["files"][name], "text": data.decode()}
            for name, data in raw.items()
        },
    }


def prepare_finalization(
    *, config, source: dict, journal_path: Path, parent_operation_id: str, validation_root: Path
) -> PreparedFinalization:
    """Verify a completed trial dossier read-only; no lease, model, or provider is touched."""
    pr_id = PRIdentity.from_url(source["url"]).key
    if any(
        p.is_symlink() for path in (journal_path, validation_root) for p in (path, *path.parents)
    ):
        raise FinalizationError("Linked recovery input path")
    journal_path, validation_root = journal_path.resolve(), validation_root.resolve()
    pr, parent, operations, refs, revisions, selected = _snapshot(
        journal_path, pr_id, parent_operation_id
    )
    if parent.key.stage != "validation" or parent.status not in {"uncertain", "failed"}:
        raise FinalizationError("Only a retained incomplete parent validation can be finalized")
    if parent.key.policy_digest != config.digest() or parent.key.input_digest != canonical_digest(
        {"task": parent.key.revision_digest}
    ):
        raise FinalizationError("Parent validation configuration or task binding changed")
    if not revisions or revisions[-1]["task_digest"] != parent.key.revision_digest:
        raise FinalizationError("Parent is not the current frozen task revision")
    if json.loads(revisions[0]["manifest"]).get("input") != source:
        raise FinalizationError("Source differs from the original registered PR input")
    ledger = json.loads(pr["ledger"])
    expected_ledger = {
        "path": str(config.ledger_path.resolve()),
        "scope": f"{config.campaign_id}:{source['id']}",
        "group": config.campaign_id,
    }
    deadline = json.loads(pr["deadline"])["expires_at"]
    if (
        ledger != expected_ledger
        or parent.ledger.model_dump() != ledger
        or parent.deadline.expires_at != deadline
    ):
        raise FinalizationError("Original ledger identity or absolute deadline changed")
    effect = "finalize:" + parent_operation_id
    children = [o for o in operations if o.key.effect == effect]
    if selected and not children:
        raise FinalizationError("Already selected PR cannot request a fresh review")
    if any(
        o.status in {"claimed", "submitted", "uncertain"}
        and o.key.operation_id != parent_operation_id
        and o.key.effect != effect
        for o in operations
    ):
        raise FinalizationError("Another unresolved child operation requires reconciliation")
    hashes = {}
    construction_candidates = []
    for op in operations:
        if op.key.stage == "construction" and op.status == "completed" and "result" in op.artifacts:
            candidate = json.loads(_blob(journal_path, op.artifacts["result"], hashes))
            if candidate.get("emitter", {}).get("task_digest") == parent.key.revision_digest:
                construction_candidates.append(candidate)
    if len(construction_candidates) != 1:
        raise FinalizationError("One exact immutable construction result is required")
    construction = construction_candidates[0]
    task = Path(construction["task_path"])
    candidate_root = Path(parent.receipt.get("stage_root", ""))
    expected_root = task.parent / (
        f"validation-attempt-{parent.key.attempt}" if parent.key.attempt else "validation"
    )
    if (
        validation_root != expected_root
        or not task.is_relative_to(candidate_root)
        or candidate_root == Path(".")
    ):
        raise FinalizationError("Validation/task paths differ from parent provenance")
    identity = _json(candidate_root / "identity.json", hashes)
    if identity != {
        "config_digest": config.digest(),
        "source_digest": canonical_digest(source),
        "deadline": deadline,
    }:
        raise FinalizationError("Candidate identity/config/deadline changed")
    task_config, contract, materialization = _task_policy(task)
    digest = digest_task(task)
    if (
        digest != parent.key.revision_digest
        or contract != Construction.model_validate(construction["artifact"]).contract
    ):
        raise FinalizationError("Task bytes or contract changed")
    # Freeze every retained file, including raw actions, exported submissions and
    # failed prior review. No generated Python is imported or executed.
    total = 0
    for directory in (task, validation_root):
        for path in sorted(directory.rglob("*")):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise FinalizationError("Nonregular retained tree entry")
            if path.is_file():
                raw = _read(path, hashes)
                total += len(raw)
                if total > 500_000_000 or len(hashes) > 30_000:
                    raise FinalizationError("Retained dossier exceeds recovery bounds")
    if any(
        (validation_root / p).exists()
        for p in ("accepted.json", "quality-report.json", "final-review/artifact.json")
    ):
        raise FinalizationError("A retained quality verdict cannot be rerolled")
    texts = {name: _blob(journal_path, ref, hashes).decode() for name, ref in refs.items()}
    if not texts.keys() >= FINAL_REQUIRED_EVIDENCE or "source_patch" not in refs:
        raise FinalizationError("Incomplete immutable final evidence inventory")
    value = Construction.model_validate(construction["artifact"])
    bundle = bind_contracts(config, source, construction, refs["source_patch"])
    if _json(validation_root / "contracts.json", hashes) != bundle.model_dump(mode="json"):
        raise FinalizationError("Retained semantic contracts changed")
    if _read(task.parent / "gold.patch", hashes).decode() != texts["source_patch"]:
        raise FinalizationError("Reference patch changed")
    comprehension = ComprehensionReview.model_validate_json(texts["comprehension"])
    critic = VerifierCritique.model_validate_json(texts["critic"])
    if comprehension.status != "pass" or critic.status != "pass":
        raise FinalizationError("Prior comprehension and verifier critic did not pass")
    for name, model in (("comprehension", comprehension), ("critic", critic)):
        artifact = _json(validation_root / name / "artifact.json", hashes)
        operation = _json(validation_root / name / "operation.json", hashes)
        if (
            artifact["artifact"] != model.model_dump(mode="json")
            or operation["status"] != "completed"
            or artifact["input_digest"] != operation["input_digest"]
        ):
            raise FinalizationError("Prior static review receipt differs from journal evidence")
    plan = _requirements(value, critic, config)
    required = [
        TrialRequirement(id=n, kind=k, expected_reward=r, model=m) for n, k, r, _, _, m in plan
    ]
    expected_refs = (
        set(FINAL_REQUIRED_EVIDENCE) | {"source_patch"} | {"trial_" + r.id for r in required}
    )
    if set(refs) != expected_refs:
        raise FinalizationError("Missing or unexpected journal trial inventory")
    inventory = _json(validation_root / "trial-inventory.json", hashes)
    if inventory["required"] != [r.model_dump(mode="json") for r in required] or set(
        inventory["outcomes"]
    ) != {r.id for r in required}:
        raise FinalizationError("Required trials differ from the frozen control/model plan")
    cpus, memory = _resources(task_config)
    profile = canonical_digest(
        {
            "provider": config.provider,
            "profile": "cpu-direct",
            "policy": 1,
            "cpus": cpus,
            "memory_mb": memory,
            "agent": task_config.environment.model_dump(mode="json"),
            "grader": task_config.verifier.environment.model_dump(mode="json"),
        }
    )
    builds = {
        role: BuildSpec.from_directory(task / directory, role=role).digest
        for role, directory in (("solver", "environment"), ("grader", "tests"))
    }
    outcomes, details_by_name = {}, {}
    ledger_state = _json(config.ledger_path)
    retained_reservations = {}
    for name, kind, reward, script, oracle, model in plan:
        folder = validation_root / "trials" / name
        outcome = TrialOutcome(**_json(folder / "outcome.json", hashes))
        operation = _json(folder / "operation.json", hashes)
        output = Path(outcome.path)
        if (
            output.parent != folder
            or operation
            != {
                "task_digest": digest,
                "config_digest": config.digest(),
                "trial": output.name,
                "deadline": deadline,
                "kind": kind,
                "status": "completed",
                "outcome_path": str(folder / "outcome.json"),
            }
            or inventory["outcomes"][name] != outcome.model_dump()
        ):
            raise FinalizationError(f"{name}: trial operation/outcome identity changed")
        if (
            outcome.task_digest != digest
            or outcome.provider != config.provider
            or outcome.profile_digest != profile
            or outcome.materialization_digest != canonical_digest(materialization)
            or outcome.kind != kind
            or outcome.model != model
            or outcome.inference_digest
            != (inference_digest(model, adversary=kind == "adversary") if model else None)
            or outcome.error
            or outcome.construction_error
            or not outcome.collection_verified
            or not outcome.cleanup_confirmed
            or outcome.reward not in (0, 1)
            or outcome.observed_reward != outcome.reward
            or (reward is not None and outcome.reward != reward)
        ):
            raise FinalizationError(f"{name}: incomplete/stale trial or wrong control outcome")
        if inspect_execution(output):
            raise FinalizationError(f"{name}: retained execution is incomplete")
        harbor = _json(folder / "harbor-config.json", hashes)
        agent = harbor["agent"]
        if (
            harbor["task"]["path"] != str(task)
            or harbor["trial_name"] != output.name
            or harbor["trials_dir"] != str(folder)
            or harbor["environment"]["type"] != config.provider
        ):
            raise FinalizationError(f"{name}: Harbor path/provider mismatch")
        kwargs = agent.get("kwargs", {})
        if model or script is not None:
            expected = {
                "budget_path": str(config.ledger_path),
                "budget_limit": config.ledger_limit_usd,
                "budget_scope": ledger["scope"],
                "scope_limit": config.candidate_limit_usd,
                "budget_group": config.campaign_id,
                "group_limit": config.campaign_limit_usd,
                "max_turns": config.solver_turns,
                "max_cost": config.solver_limit_usd,
                "mode": "adversary" if kind == "adversary" else ("solve" if model else "script"),
            }
            if script is not None:
                expected["script"] = script
            if oracle:
                expected["oracle_dir"] = str(task / "solution")
            if kwargs != expected or agent.get("model_name") != model:
                raise FinalizationError(f"{name}: model/script/budget changed")
        elif agent.get("name") != "oracle":
            raise FinalizationError(f"{name}: reference agent changed")
        checksum, details = _inspect_collection(
            output, _json(folder / "pretransfer.json", hashes), contract
        )
        receipt = _json(folder / "collection-receipt.json", hashes)
        if (
            receipt
            != {
                "task_digest": digest,
                "inventory_digest": checksum,
                "profile_digest": profile,
                "verified": True,
            }
            or details.get("reward") != outcome.reward
            or details.get("outcome") != ("passed" if outcome.reward else "submission_failure")
        ):
            raise FinalizationError(f"{name}: protected reward/collection disagreement")
        resource_deadline = harbor["environment"]["kwargs"]["absolute_deadline"]
        cleaned, resources = _cleanup_receipts(
            folder / "resources", config.provider, resource_deadline, builds
        )
        if (
            not cleaned
            or len(resources) != 2
            or {r["role"] for r in resources} != {"solver", "grader"}
            or len({r["resource_id"] for r in resources}) != 2
            or resource_deadline > deadline
        ):
            raise FinalizationError(f"{name}: incomplete or inconsistent resource cleanup")
        reservation = _json(folder / "reservation.json", hashes)
        entry = ledger_state["entries"].get(outcome.reservation_id)
        if (
            not entry
            or entry["status"] not in {"estimated", "metered"}
            or entry.get("scope") != ledger["scope"]
            or entry.get("group") != ledger["group"]
            or reservation.get("id") != outcome.reservation_id
            or reservation.get("resources") != resources
            or reservation.get("charged_usd") != entry["charged_usd"]
            or outcome.cloud_cost_estimated_usd != entry["charged_usd"]
        ):
            raise FinalizationError(f"{name}: original cloud reservation/charges missing")
        retained_reservations[outcome.reservation_id] = entry
        observation_path = output / "verifier/source-observation.json"
        observation = _json(observation_path, hashes) if observation_path.exists() else None
        if outcome.source_observation != observation:
            raise FinalizationError(f"{name}: source observation changed")
        outcomes[name], details_by_name[name] = outcome, details
    witness = paired_witness(outcomes["baseline"], outcomes["oracle-0"])
    if not witness["passed"] or witness != _json(validation_root / "source-witness.json", hashes):
        raise FinalizationError("Paired source materialization witness no longer verifies")
    for outcome in outcomes.values():
        outcome.materialization_verified = True
    expected_texts = _texts(task, construction, bundle, outcomes, witness, comprehension, critic)
    expected_texts.update({"trial_" + name: _trial_text(row) for name, row in outcomes.items()})
    for name, expected in expected_texts.items():
        # Construction's durable JSON uses sorted object keys. Preserve the exact
        # captured model text while permitting that lossless serialization change.
        matches = (
            json.loads(texts[name]) == json.loads(expected)
            if name in {"contract", "bootstrap", "comprehension", "critic", "source_witness"}
            else texts[name] == expected
        )
        if not matches:
            raise FinalizationError(
                f"{name}: retained evidence differs from exact source/trial receipts"
            )
    old_review = _json(validation_root / "final-review/operation.json", hashes)
    if old_review.get("status") != "incomplete" or old_review.get("deadline") != deadline:
        raise FinalizationError("Prior review is not a stopped incomplete operation")
    cost = old_review.get("charged_or_reserved_usd")
    trace = [
        json.loads(line)
        for line in _read(validation_root / "final-review/trace.jsonl", hashes)
        .decode()
        .splitlines()
    ]
    recorded = sum(row.get("cost_usd", 0) for row in trace if row.get("kind") == "model")
    if (
        type(cost) not in (int, float)
        or not math.isfinite(cost)
        or cost < 0
        or not math.isclose(recorded, cost, abs_tol=1e-8)
    ):
        raise FinalizationError("Prior review charges are incomplete or inconsistent")
    records = [
        TrialRecord(
            id=name,
            revision_digest=digest,
            kind=row.kind,
            outcome="passed" if row.reward else "submission_failure",
            reward=int(row.reward),
            environment_healthy=True,
            collection_complete=True,
            materialization_verified=True,
            checks_run=details_by_name[name].get("n_tests", 0),
            evidence_ids=["trial_" + name, "source_witness"],
            diagnostic="Protected result and exact transferred source verified",
            model=row.model,
        )
        for name, row in outcomes.items()
    ]
    context = AdmissionContext(
        pr=PRIdentity.from_url(source["url"]),
        revision_digest=digest,
        required_trials=required,
        trials=records,
        evidence={
            name: EvidenceRef(revision_digest=digest, artifact=ref) for name, ref in refs.items()
        },
        execution_profile_validated=True,
        artifact_integrity_verified=True,
    )
    proof = {
        "policy_version": POLICY_VERSION,
        "parent_operation": parent.model_dump(mode="json"),
        "journal_path": str(journal_path),
        "validation_root": str(validation_root),
        "task_path": str(task),
        "config_digest": config.digest(),
        "source_digest": canonical_digest(source),
        "deadline": deadline,
        "prior_review_charge_usd": cost,
        "files": hashes,
        "reservations": retained_reservations,
        "context": context.model_dump(mode="json"),
    }
    return PreparedFinalization(
        journal_path,
        parent_operation_id,
        validation_root,
        source,
        task,
        deadline,
        cost,
        {name: texts[name] for name in FINAL_REQUIRED_EVIDENCE},
        context,
        proof,
    )


async def finalize_review_only(
    prepared: PreparedFinalization,
    *,
    config,
    journal,
    lease,
    root: Path,
    review_callback,
    review_policy: dict,
) -> dict:
    """Own one fresh review effect, retain parent charges, and apply ordinary admission gates."""
    fresh = prepare_finalization(
        config=config,
        source=prepared.source,
        journal_path=journal.path,
        parent_operation_id=prepared.parent_operation_id,
        validation_root=prepared.validation_root,
    )
    if fresh.digest != prepared.digest or journal.path.resolve() != prepared.journal_path:
        raise FinalizationError("Prepared evidence changed before finalization")
    root = root.resolve()
    if root.is_relative_to(prepared.validation_root) or root.is_relative_to(prepared.task_path):
        raise FinalizationError("Finalization must use a separate new evidence root")
    key = OperationKey(
        pr_id=lease.pr_id,
        revision_digest=fresh.context.revision_digest,
        stage="review_finalization",
        effect="finalize:" + fresh.parent_operation_id,
        attempt=0,
        input_digest=canonical_digest({"prepared": fresh.digest, "root": str(root)}),
        policy_digest=canonical_digest({"version": POLICY_VERSION, "review": review_policy}),
    )
    matches = [o for o in journal.operations(lease.pr_id) if o.key.effect == key.effect]
    if matches and (len(matches) != 1 or matches[0].key != key):
        raise ReconciliationRequired(
            "Parent already has a differently bound finalization; no reroll"
        )
    claim = journal.claim_operation(lease, key)
    if not claim.acquired:
        record = claim.operation
        # A complete durable result is recoverable without another review even
        # if interruption happened just before selection/operation completion.
        with sqlite3.connect(journal.path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            refs = {
                name: ArtifactRef.model_validate_json(ref)
                for name, ref in connection.execute(
                    "SELECT name,reference FROM artifacts WHERE operation_id=? AND status='committed'",
                    (key.operation_id,),
                )
            }
        expected_names = {
            "prepared",
            "report",
            "context",
            "result",
        }
        if "review_delivery" in refs or review_policy.get("delivery_policy"):
            expected_names.add("review_delivery")
        if record.status not in {"completed", "uncertain"} or set(refs) != expected_names:
            raise ReconciliationRequired(
                "Incomplete finalization needs reconciliation; never rerun review"
            )
        values = {
            name: json.loads(journal.verify_artifact(ref).read_text()) for name, ref in refs.items()
        }
        if values["prepared"] != fresh.proof or values["context"] != fresh.context.model_dump(
            mode="json"
        ):
            raise FinalizationError("Retained finalization context changed")
        report = QualityReport.model_validate(values["report"])
        delivered = _review_delivery(root, report, fresh, config, review_policy)
        if values.get("review_delivery") != delivered:
            raise FinalizationError("Retained review delivery differs from its committed receipt")
        reasons = admission_reasons(report, fresh.context)
        result = values["result"]
        if result["reasons"] != reasons or result["status"] != (
            "needs_repair" if reasons else "accepted"
        ):
            raise FinalizationError("Retained finalization result disagrees with admission")
        if not reasons:
            journal.select_revision(lease, report=report, context=fresh.context)
        if record.status == "uncertain":
            journal.reconcile_operation(
                lease,
                key.operation_id,
                receipt={
                    "result": refs["result"].model_dump(mode="json"),
                    "parent_operation_id": fresh.parent_operation_id,
                },
            )
        return result
    budget = config.budget(fresh.source["id"])
    started = budget.spent
    try:
        if fresh.prior_review_charge_usd >= config.review_stage_limit_usd:
            raise BudgetExceeded("Original final-review allowance exhausted")
        if root.exists() and any(root.iterdir()):
            raise FinalizationError("Fresh finalization root must be empty")
        journal.commit_artifact(
            lease, key.operation_id, name="prepared", data=json.dumps(fresh.proof).encode()
        )
        report = await review_callback(
            config=config,
            budget=budget,
            root=root,
            deadline=fresh.deadline,
            pr=fresh.context.pr,
            revision_digest=fresh.context.revision_digest,
            evidence=fresh.evidence,
            prior_review_charge_usd=fresh.prior_review_charge_usd,
        )
        report = QualityReport.model_validate(report)
        delivered = _review_delivery(root, report, fresh, config, review_policy)
        if (
            budget.spent - started + fresh.prior_review_charge_usd
            > config.review_stage_limit_usd + 1e-8
        ):
            raise BudgetExceeded(
                "Combined original and resumed review charges exceed the original allowance"
            )
        after = prepare_finalization(
            config=config,
            source=fresh.source,
            journal_path=journal.path,
            parent_operation_id=fresh.parent_operation_id,
            validation_root=fresh.validation_root,
        )
        if after.digest != fresh.digest:
            raise FinalizationError("Frozen evidence changed during review")
        reasons = admission_reasons(report, fresh.context)
        result = {
            "status": "needs_repair" if reasons else "accepted",
            "repairable": bool(reasons),
            "stage": "final_review",
            "task_path": str(fresh.task_path),
            "task_digest": fresh.context.revision_digest,
            "parent_operation_id": fresh.parent_operation_id,
            "finalization_operation_id": key.operation_id,
            "reasons": reasons,
            "review": report.model_dump(mode="json"),
            "prior_review_charge_usd": fresh.prior_review_charge_usd,
            "new_review_charge_usd": budget.spent - started,
        }
        committed = {}
        if delivered is not None:
            journal.commit_artifact(
                lease, key.operation_id, name="review_delivery", data=json.dumps(delivered).encode()
            )
        for name, value in (
            ("report", report.model_dump(mode="json")),
            ("context", fresh.context.model_dump(mode="json")),
            ("result", result),
        ):
            committed[name] = journal.commit_artifact(
                lease, key.operation_id, name=name, data=json.dumps(value).encode()
            )
        if not reasons:
            journal.select_revision(lease, report=report, context=fresh.context)
        journal.finish_operation(
            lease,
            key.operation_id,
            receipt={
                "result": committed["result"].model_dump(mode="json"),
                "parent_operation_id": fresh.parent_operation_id,
            },
        )
        return result
    except BaseException as exc:
        journal.mark_uncertain(
            lease,
            key.operation_id,
            reason=f"{type(exc).__name__}: {exc}",
            receipt={"parent_operation_id": fresh.parent_operation_id, "output_root": str(root)},
        )
        raise
