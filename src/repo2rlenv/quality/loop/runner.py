"""A bounded, resumable quality loop with injectable model and trial adapters."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger
from repo2rlenv.campaigns.events import EventJournal, ProgressEvent
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.loop.artifacts import (
    apply_repair,
    import_trial,
    parse_task,
    probe_variant,
    snapshot,
    task_identity,
)
from repo2rlenv.quality.loop.client import JsonModel, ModelRequestError, RunBudget, prompt
from repo2rlenv.quality.loop.context import EvidenceContext
from repo2rlenv.quality.loop.models import (
    LoopOptions,
    LoopResult,
    ProbeManifest,
    ReadRequest,
    Repair,
    Review,
    SemanticProbe,
    TrialRecord,
)
from repo2rlenv.quality.loop.protocol import (
    distinct_probes,
    resolve_json_citations,
    resolve_markdown_citations,
    resolve_verifier_paths,
)
from repo2rlenv.quality.loop.requirements import task_probe_focus


def required_probe_focus(task: Path) -> set[str]:
    """Narrow explicit contracts learned from actual pilot false acceptances."""
    instruction = (task / "instruction.md").read_text().lower()
    focus: set[str] = set(task_probe_focus(task))
    if re.search(r"\blaz(?:y|ily)\b|\bgenerator function\b|\breturn a generator\b", instruction):
        focus.add("lazy_output")
    if re.search(r"\b(?:absolute|relative) error\b|\bnumerical? tolerance\b", instruction):
        focus.add("numeric_tolerance")
    return focus


def control_failures(trials: list[TrialRecord], success: float) -> list[str]:
    failures = []
    for role in ("baseline", "oracle"):
        records = [item for item in trials if item.role == role]
        if not records:
            failures.append(f"Missing {role} execution evidence")
            continue
        item = records[-1]
        if item.exception_type or item.agent_exit_code not in {None, 0} or item.reward is None:
            failures.append(f"{role} did not execute cleanly")
        elif (role == "oracle" and item.reward != success) or (
            role == "baseline" and item.reward >= success
        ):
            failures.append(f"{role} reward {item.reward} violates the baseline/reference contrast")
    return failures


def probe_failures(trials: list[TrialRecord], success: float) -> list[str]:
    failures = []
    for trial in trials:
        if trial.role != "probe":
            continue
        if (
            not trial.probe_installed
            or trial.exception_type
            or trial.agent_exit_code not in {None, 0}
            or trial.reward is None
        ):
            failures.append(f"Probe {trial.probe.name} did not install/execute cleanly")
        elif (trial.probe.kind == "valid_alternative" and trial.reward != success) or (
            trial.probe.kind == "wrong_solution" and trial.reward >= success
        ):
            failures.append(f"Probe {trial.probe.name}: {trial.probe.kind} earned {trial.reward}")
    return failures


def _reusable_probe_trial(
    trial: TrialRecord, probe: SemanticProbe, parent_hash: str, success: float
) -> bool:
    if trial.role != "probe" or trial.probe != probe or probe_failures([trial], success):
        return False
    # Use the publication gate's raw-result, completion-marker, controller
    # receipt and probe-parent checks. A successful summary alone is insufficient.
    from repo2rlenv.quality.labels import _trial_evidence

    _trial_evidence(trial, parent_hash)
    return True


class QualityLoop:
    """Generation-independent component; task code runs only through trial_runner.

    Adapters implement model_client.ask(schema, model, system, user, key) and
    trial_runner.run(task, role, key). The latter also exposes close(). A caller
    may supply existing evidence without creating a remote runner at all.
    """

    def __init__(
        self,
        options: LoopOptions,
        directory: Path,
        ledger: BudgetLedger,
        *,
        budget: RunBudget | None = None,
        protected_paths: tuple[str, ...] = (),
        task_context: dict | None = None,
        model_client=None,
        trial_runner=None,
        on_event: Callable[[ProgressEvent], None] | None = None,
    ):
        self.options, self.directory = options, directory.resolve()
        self.protected_paths = tuple(Path(value) for value in protected_paths)
        self.task_context = task_context
        prefix = "quality-" + hashlib.sha256(str(self.directory).encode()).hexdigest()[:16]
        self.budget = budget or RunBudget(ledger, prefix, options.max_spend_usd)
        self.model = model_client or JsonModel(
            self.directory / "calls",
            self.budget,
            reservation=options.model_reservation_usd,
            max_tokens=options.model_tokens,
        )
        self.remote = trial_runner
        self.on_event = on_event or (lambda event: None)

    def event(self, stage: str, message: str, *, state: str = "progress"):
        event = ProgressEvent(recipe="quality", stage=stage, state=state, message=message)
        EventJournal(self.directory / "events.jsonl").emit(event)
        self.on_event(event)

    def _context(self, task, trials, **extra):
        identity = task_identity(task)
        if any(trial.bundle_hash != identity for trial in trials if trial.role != "probe"):
            raise ValueError("Execution evidence belongs to another task revision")
        context = EvidenceContext(task, trials, limit=self.options.context_chars)
        if self.task_context is not None:
            context.add_text(
                "evidence/task-context.json",
                json.dumps(self.task_context, indent=2),
                maximum=min(32000, self.options.context_chars // 4),
            )
        context.documents["evidence/checks.json"] = json.dumps(
            {
                "control_failures": control_failures(trials, self.options.success_reward),
                "probe_failures": probe_failures(trials, self.options.success_reward),
                **extra,
            },
            indent=2,
        )
        return context

    def _review(
        self,
        task,
        trials,
        key,
        *,
        probe_limit: int,
        prior: Review | None = None,
        existing_probes: list[SemanticProbe] | None = None,
        revision: int = 0,
    ):
        context = self._context(task, trials)
        save_record(
            self.directory / "inventories" / f"{key}.json",
            {"bundle_hash": task_identity(task), "files": context.inventory},
        )
        existing = existing_probes or []
        needed_focus = required_probe_focus(task) - {
            probe.focus for probe in existing if probe.kind == "wrong_solution"
        }
        needed_kinds = {"wrong_solution", "valid_alternative"} - {probe.kind for probe in existing}
        self.event(
            "review", "Review task, verifier and available execution evidence", state="started"
        )
        feedback = []
        review = None
        model = self.options.review_model
        rounds = self.options.max_read_rounds + 1
        for index in range(rounds + int(self.options.escalation_model is not None)):
            if index == rounds:
                model = self.options.escalation_model
                self.event("review", "Escalate unresolved evidence to the configured model")
            # The latest parsed draft guides bounded corrections and follow-up
            # reads; it never substitutes for validating the next response.
            previous = review if review is not None else prior
            try:
                review = self.model.ask(
                    Review,
                    model,
                    prompt("review"),
                    context.payload(
                        probe_limit=probe_limit,
                        repair_rounds_remaining=max(0, self.options.max_repairs - revision),
                        max_repair_rounds=self.options.max_repairs,
                        review_calls_remaining=max(0, rounds - index - 1),
                        protected_paths=[str(path) for path in self.protected_paths],
                        required_probe_focus=sorted(needed_focus),
                        required_probe_kinds=sorted(needed_kinds) if probe_limit else [],
                        retained_probes=[
                            {"name": p.name, "kind": p.kind, "focus": p.focus} for p in existing
                        ],
                        protocol_feedback=feedback,
                        previous_review=previous.model_dump() if previous is not None else None,
                    ),
                    f"{key}-{index}",
                )
                review, corrections = resolve_json_citations(review, context.documents)
                review, markdown_corrections = resolve_markdown_citations(review, context.documents)
                corrections.extend(markdown_corrections)
                if corrections:
                    save_record(
                        self.directory / "protocol" / f"{key}-{index}-citations.json",
                        {"corrections": corrections},
                    )
                context.validate_review(review)
                if review.read_requests:
                    context.read_more(review.read_requests)
                    feedback.append(
                        "Requested file ranges are now included; finish the review if sufficient."
                    )
                    continue
                normalized = distinct_probes(review.probes, existing)
                if normalized != review.probes:
                    save_record(
                        self.directory / "protocol" / f"{key}-{index}-probes.json",
                        {
                            "proposed": [p.model_dump() for p in review.probes],
                            "normalized": [p.model_dump() for p in normalized],
                        },
                    )
                    review = review.model_copy(update={"probes": normalized})
                if len(review.probes) > probe_limit:
                    raise ValueError("Proposed probes exceed the remaining probe limit")
                if probe_limit and review.sound:
                    missing = needed_focus - {
                        probe.focus for probe in review.probes if probe.kind == "wrong_solution"
                    }
                    if missing:
                        raise ValueError(
                            f"Propose a wrong-solution probe targeting these explicit requirements: {sorted(missing)}"
                        )
                    # A bounded review with only one slot may still be useful,
                    # but two or more slots must cover both semantic controls.
                    required_count = max(
                        len(needed_focus), int("wrong_solution" in needed_kinds)
                    ) + int("valid_alternative" in needed_kinds)
                    missing_kinds = needed_kinds - {probe.kind for probe in review.probes}
                    if required_count <= probe_limit and missing_kinds:
                        raise ValueError(
                            f"Reserve probe slots for missing control kinds: {sorted(missing_kinds)}"
                        )
                save_record(self.directory / "reviews" / f"{key}.json", review.model_dump())
                return review, context
            except (ValidationError, ValueError) as exc:
                feedback.append(f"Invalid structured review or evidence request: {exc}")
        raise ValueError(
            "Review did not resolve its evidence requests or grounded output within the call limit: "
            + (feedback[-1] if feedback else "no grounded response")
        )

    def _repair(self, task, review, context, probes, revision, reasons):
        feedback = []
        for attempt in range(2):
            repair = None
            invalid_citation = None
            try:
                repair = self.model.ask(
                    Repair,
                    self.options.repair_model or self.options.review_model,
                    prompt("repair"),
                    context.payload(
                        review=review.model_dump(),
                        repair_round=revision + 1,
                        max_repair_rounds=self.options.max_repairs,
                        repair_rounds_remaining=max(0, self.options.max_repairs - revision - 1),
                        failures=reasons,
                        retained_probes=[probe.model_dump() for probe in probes],
                        patch_feedback=feedback,
                        protected_paths=[str(path) for path in self.protected_paths],
                    ),
                    f"r{revision}-repair" + (f"-correction{attempt}" if attempt else ""),
                )
                normalized = resolve_verifier_paths(task, repair)
                if normalized != repair:
                    save_record(
                        self.directory / "protocol" / f"r{revision}-repair{attempt}-paths.json",
                        {"proposed": repair.model_dump(), "normalized": normalized.model_dump()},
                    )
                    repair = normalized
                for edit in repair.edits:
                    if any(
                        Path(edit.path) == path or Path(edit.path).is_relative_to(path)
                        for path in self.protected_paths
                    ):
                        raise ValueError(f"Repair changes immutable source or oracle: {edit.path}")
                updated_probes = probes
                if repair.probe_replacements:
                    if not any(issue.category == "probe" for issue in review.issues):
                        raise ValueError(
                            "Alternative-probe replacement requires a grounded probe diagnosis"
                        )
                    replacements = {probe.name: probe for probe in repair.probe_replacements}
                    if len(replacements) != len(repair.probe_replacements):
                        raise ValueError("Duplicate probe replacements")
                    known = {probe.name: probe for probe in probes}
                    for replacement_index, (name, replacement) in enumerate(replacements.items()):
                        if (
                            name not in known
                            or known[name].kind != "valid_alternative"
                            or known[name].focus != replacement.focus
                        ):
                            raise ValueError(
                                "Probe repair must preserve its name, kind and requirement focus"
                            )
                        for citation_index, citation in enumerate(replacement.evidence):
                            document = context.documents.get(citation.path, "")
                            if not document or " ".join(citation.quote.split()) not in " ".join(
                                document.split()
                            ):
                                invalid_citation = citation
                                raise ValueError(
                                    "Alternative-probe replacement has ungrounded evidence at "
                                    f"probe_replacements[{replacement_index}] ({name})"
                                    f".evidence[{citation_index}]: path={citation.path!r}, "
                                    f"quote={citation.quote!r}. Use an exact quote from a cited "
                                    "document, including traceback markers, or cite another "
                                    "existing document."
                                )
                    updated_probes = [replacements.get(probe.name, probe) for probe in probes]
                destination = self.directory / f"revisions/r{revision + 1}" / task.name
                if destination.exists():
                    saved = json.loads((destination.parent / "repair.json").read_text())
                    if (
                        saved["parent_hash"] != task_identity(task)
                        or saved["repair"] != repair.model_dump()
                    ):
                        raise ValueError("Stored repair differs from the requested revision")
                    if task_identity(destination) != saved["bundle_hash"]:
                        raise ValueError("Stored repair has changed")
                else:
                    apply_repair(task, repair, destination)
                if task_probe_focus(task) - task_probe_focus(destination):
                    raise ValueError("Repair removed explicit task probe requirements")
                return destination, updated_probes
            except (ValueError, FileNotFoundError, FileExistsError) as exc:
                if attempt:
                    raise
                feedback.append(str(exc))
                if invalid_citation is not None and (
                    invalid_citation.path in context._paths
                    or invalid_citation.path in context._texts
                ):
                    # Search only the known cited source, preserving its literal
                    # traceback markers in the existing single correction call.
                    query = next(
                        (
                            line.strip()
                            for line in invalid_citation.quote.splitlines()
                            if line.strip()
                        ),
                        None,
                    )
                    try:
                        context.read_more(
                            [
                                ReadRequest(
                                    path=invalid_citation.path,
                                    query=query[:200] if query else None,
                                    start_line=1,
                                    end_line=80 if query is None else 1,
                                )
                            ]
                        )
                    except ValueError as read_error:
                        feedback.append(f"Cited-source excerpt unavailable: {read_error}")
                if repair is not None:
                    # Supply real source around the requested edit, never a fuzzy
                    # application. Unknown provider effects bypass this correction.
                    for edit in repair.edits:
                        if edit.path in context._paths:
                            query = next(
                                (line.strip() for line in edit.old.splitlines() if line.strip()),
                                None,
                            )
                            context.read_more(
                                [
                                    ReadRequest(
                                        path=edit.path,
                                        query=query[:200] if query else None,
                                        start_line=1,
                                        end_line=200 if query is None else 1,
                                    )
                                ]
                            )
                self.event("repair", "Correct one invalid patch proposal using actual source text")
        raise RuntimeError("Unreachable repair state")

    def run(
        self,
        task: Path,
        *,
        baseline: Path | None = None,
        oracle: Path | None = None,
        rollout: Path | None = None,
        probes: Path | None = None,
        resume: bool = False,
    ) -> LoopResult:
        task = task.absolute()
        if self.directory == task or self.directory.is_relative_to(task):
            raise ValueError("Quality output must be outside the input task")
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another controller owns this quality run") from exc
            try:
                return self._run(task, baseline, oracle, rollout, probes, resume)
            finally:
                if self.remote is not None:
                    self.remote.close()

    def _run(self, source, baseline, oracle, rollout, probe_file, resume):
        source_hash = task_identity(source)
        known_probes = (
            ProbeManifest.model_validate_json(probe_file.read_text()) if probe_file else None
        )
        if known_probes and (
            known_probes.bundle_hash != source_hash
            or len(known_probes.probes) > self.options.max_probes
        ):
            raise ValueError("Known probes must match the task and fit within --max-probes")
        imported = [
            import_trial(path, source, role)
            for role, path in (("baseline", baseline), ("oracle", oracle), ("rollout", rollout))
            if path
        ]
        configuration = {
            "protected_paths": [str(path) for path in self.protected_paths],
            "task_context": self.task_context,
            "source_hash": source_hash,
            "source": str(source),
            "options": self.options.model_dump(mode="json"),
            "evidence": [trial.model_dump() for trial in imported],
            "known_probes": known_probes.model_dump() if known_probes else None,
            "evidence_files": [
                item
                for item in EvidenceContext(
                    source, imported, limit=self.options.context_chars
                ).inventory
                if item["path"].startswith("evidence/")
            ],
            "execution": self.remote.identity() if hasattr(self.remote, "identity") else None,
            "ledger": str(self.budget.path.resolve()),
            "prompts": {
                name: hashlib.sha256(prompt(name).encode()).hexdigest()
                for name in ("review", "repair")
            },
        }
        receipt = self.directory / "run.json"
        if receipt.exists():
            if not resume or json.loads(receipt.read_text())["configuration"] != configuration:
                raise ValueError(
                    "Existing quality run requires --resume with identical input and configuration"
                )
        else:
            save_record(receipt, {"configuration": configuration, "state": "running"})
        task = snapshot(source, self.directory / "revisions/r0" / source.name)
        trials, review, revision, reasons = imported, None, 0, []
        probes = list(known_probes.probes) if known_probes else []
        status = "needs_evidence"
        try:
            for revision in range(self.options.max_repairs + 1):
                self.event("revision", f"Inspect revision {revision}", state="started")
                # A package parse is read-only; Docker is exclusively in the remote adapter.
                parse_task(task)
                config = tomllib.loads((task / "task.toml").read_text())
                execute = self.remote is not None and (
                    self.options.run_rollout or self.options.repair
                )
                if execute and (
                    config.get("environment", {}).get("network_mode") != "no-network"
                    or (task / "environment/docker-compose.yaml").exists()
                ):
                    reasons = [
                        "Remote validation currently requires a single-container no-network Dockerfile task"
                    ]
                    break
                if execute:
                    for role in ("baseline", "oracle"):
                        if not any(item.role == role for item in trials):
                            self.event(role, f"Run fresh {role} control", state="started")
                            trials.append(self.remote.run(task, role, f"r{revision}-{role}"))
                            if trials[-1].exception_type == "BudgetExceeded":
                                raise BudgetExceeded("Control allocation denied")
                before = len(trials)
                review, context = self._review(
                    task,
                    trials,
                    f"r{revision}-before",
                    probe_limit=max(0, self.options.max_probes - len(probes)),
                    existing_probes=probes,
                    revision=revision,
                )
                if len(probes) < self.options.max_probes:
                    names = {probe.name for probe in probes}
                    for probe in review.probes:
                        if probe.name not in names and len(probes) < self.options.max_probes:
                            probes.append(probe)
                            names.add(probe.name)
                controls = control_failures(trials, self.options.success_reward)
                if execute and not controls:
                    parent_hash = task_identity(task)
                    for index, probe in enumerate(probes):
                        if any(
                            _reusable_probe_trial(
                                trial, probe, parent_hash, self.options.success_reward
                            )
                            for trial in trials
                        ):
                            self.event("probe", f"Reuse unchanged {probe.kind}: {probe.name}")
                            continue
                        key = f"r{revision}-probe{index}"
                        destination = self.directory / "probes" / key / source.name
                        if not destination.exists():
                            probe_variant(task, probe, destination)
                        else:
                            saved = json.loads((destination.parent / "probe.json").read_text())
                            if saved != {
                                "parent_hash": task_identity(task),
                                "bundle_hash": task_identity(destination),
                                "probe": probe.model_dump(),
                            }:
                                raise ValueError("Stored semantic probe changed")
                        self.event("probe", f"Run {probe.kind}: {probe.name}", state="started")
                        result = self.remote.run(destination, "probe", key)
                        trials.append(result.model_copy(update={"probe": probe}))
                        if result.exception_type == "BudgetExceeded":
                            raise BudgetExceeded("Probe allocation denied")
                    if (
                        review.sound
                        and not probe_failures(trials, self.options.success_reward)
                        and not any(item.role == "rollout" for item in trials)
                    ):
                        self.event(
                            "rollout", "Run a blind solver on this revision", state="started"
                        )
                        trials.append(self.remote.run(task, "rollout", f"r{revision}-rollout"))
                        if trials[-1].exception_type == "BudgetExceeded":
                            raise BudgetExceeded("Rollout allocation denied")
                if len(trials) != before:
                    review, context = self._review(
                        task,
                        trials,
                        f"r{revision}-after",
                        probe_limit=0,
                        prior=review,
                        revision=revision,
                    )
                reasons = [*controls, *probe_failures(trials, self.options.success_reward)]
                reasons += [
                    issue.problem for issue in review.issues if issue.severity == "blocking"
                ]
                defect = bool(reasons) or not review.sound
                if not defect:
                    kinds = {item.probe.kind for item in trials if item.role == "probe"}
                    solver = [item for item in trials if item.role == "rollout"]
                    if not solver:
                        reasons.append("No blind rollout on this revision")
                    elif review.rollout not in {"legitimate_success", "legitimate_failure"}:
                        reasons.append("Rollout outcome still needs diagnosis")
                    elif solver[-1].exception_type not in {None, "AgentTimeoutError"}:
                        reasons.append("Solver encountered an infrastructure failure")
                    if kinds != {"wrong_solution", "valid_alternative"}:
                        reasons.append(
                            "A wrong-solution control and a valid-alternative control are required for usable status"
                        )
                    covered = {
                        item.probe.focus
                        for item in trials
                        if item.role == "probe" and item.probe.kind == "wrong_solution"
                    }
                    if required_probe_focus(task) - covered:
                        reasons.append("Explicit requirement probes have not been executed")
                    status = "reviewed" if reasons else "usable"
                    break
                missing_only = all(reason.startswith("Missing ") for reason in reasons)
                if missing_only and review.sound:
                    status = "reviewed"
                    break
                status = "needs_repair"
                if not reasons and not any(issue.severity == "blocking" for issue in review.issues):
                    status = "needs_evidence"
                    reasons = ["Review is unresolved without a concrete, grounded repair"]
                    break
                if not self.options.repair or revision >= self.options.max_repairs:
                    break
                if not review.issues and any("execute cleanly" in reason for reason in reasons):
                    status = "needs_evidence"
                    break
                self.event("repair", f"Author targeted repair {revision + 1}", state="started")
                destination, probes = self._repair(task, review, context, probes, revision, reasons)
                parent_hash = task_identity(destination)
                same_task = parent_hash == task_identity(task)
                task = destination
                trials = (
                    [
                        trial
                        for trial in trials
                        if trial.role != "probe"
                        or any(
                            _reusable_probe_trial(
                                trial, probe, parent_hash, self.options.success_reward
                            )
                            for probe in probes
                        )
                    ]
                    if same_task
                    else []
                )
        except BudgetExceeded:
            status, reasons = (
                "budget_exhausted",
                ["Per-run or campaign allowance exhausted; completed evidence retained"],
            )
        except (ValueError, FileNotFoundError, ModelRequestError) as exc:
            status, reasons = "needs_evidence", [str(exc)]
        finally:
            if task_identity(source) != source_hash:
                raise ValueError("Original task changed during quality processing")
        result = LoopResult(
            status=status,
            source_hash=source_hash,
            bundle_hash=task_identity(task),
            task_path=str(task),
            repairs=revision,
            review=review,
            trials=trials,
            reasons=reasons,
            **self.budget.totals(),
        )
        save_record(self.directory / "result.json", result.model_dump(mode="json"))
        save_record(
            receipt, {"configuration": configuration, "state": "completed", "status": status}
        )
        from repo2rlenv.quality.loop.publication import publish_label

        publication = publish_label(self.directory)
        self.event(
            "label",
            f"Labeled task export: {publication['state']}",
            state="completed" if publication["state"] == "completed" else "failed",
        )
        self.event("result", f"{status}: {revision} repairs", state="completed")
        return result
