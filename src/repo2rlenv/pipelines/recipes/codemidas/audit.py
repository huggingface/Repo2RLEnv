"""Paper-style blind audit and separate mixed-outcome curriculum screening."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.execution.artifacts import check_runtime_wheel, install_runtime, runtime_python
from repo2rlenv.execution.base import connect_worker
from repo2rlenv.execution.harbor import (
    abandon_undispatched_trial,
    read_trial,
    recover_trial,
    run_trial,
)
from repo2rlenv.execution.lifecycle import prepare_docker, save_record
from repo2rlenv.pipelines.recipes.codemidas.models import Artifact
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.spec.input import LLMSpec
from repo2rlenv.tasksmith.author.artifact import artifact_stage
from repo2rlenv.tasksmith.author.budget import AuthorBudget


class Judgment(Artifact):
    trial: str
    outcome: Literal[
        "legitimate_success",
        "legitimate_failure",
        "false_positive",
        "false_negative",
        "infrastructure",
        "uncertain",
    ]
    explanation: str = Field(min_length=30, max_length=2500)
    evidence: list[str] = Field(min_length=1)


class AuditReview(Artifact):
    sound: bool
    solver_sound: bool | None = None
    exploit_confirmed: bool
    exploit_explanation: str = Field(min_length=30, max_length=3000)
    judgments: list[Judgment] = Field(min_length=4, max_length=4)
    issues: list[str]

    @model_validator(mode="after")
    def claims(self):
        if self.solver_sound is None:
            self.solver_sound = self.sound
        if {item.trial for item in self.judgments} != {f"solve-{i}" for i in range(4)}:
            raise ValueError("Judge all four independent solution attempts exactly once")
        if self.sound and (
            not self.solver_sound
            or self.exploit_confirmed
            or self.issues
            or any(
                item.outcome not in {"legitimate_success", "legitimate_failure"}
                for item in self.judgments
            )
        ):
            raise ValueError("A sound task cannot have unresolved audit defects")
        if self.solver_sound and any(
            item.outcome not in {"legitimate_success", "legitimate_failure"}
            for item in self.judgments
        ):
            raise ValueError("Solver agreement requires four conclusive legitimate outcomes")
        return self


def curriculum(rewards: list[float | None]) -> str:
    if not rewards or any(isinstance(value, bool) or value not in {0, 1} for value in rewards):
        return "incomplete"
    if all(value == 1 for value in rewards):
        return "all_pass"
    if all(value == 0 for value in rewards):
        return "all_fail"
    return "mixed"


def validate_review(review: AuditReview, index: dict, summaries: list[dict]):
    """Reject invented evidence paths and judgments that contradict rewards."""
    attempts = {item["trial"]: item for item in summaries}
    if review.sound and attempts["exploit"]["exception"] is not None:
        raise ValueError("An incomplete adversarial attempt cannot establish soundness")
    expected_rewards = {
        "legitimate_success": 1,
        "false_positive": 1,
        "legitimate_failure": 0,
        "false_negative": 0,
    }
    for judgment in review.judgments:
        attempt = attempts[judgment.trial]
        if not any(path.startswith(f"attempts/{judgment.trial}/") for path in judgment.evidence):
            raise ValueError("Each judgment must cite evidence from its own attempt")
        if judgment.outcome in expected_rewards and (
            attempt["exception"] is not None
            or attempt["reward"] != expected_rewards[judgment.outcome]
        ):
            raise ValueError("Judgment contradicts its recorded reward or execution exception")
        for citation in judgment.evidence:
            match = re.fullmatch(r"(.+?)(?::(\d+)(?:-(\d+))?)?", citation)
            path, first, last = match.groups()
            if path not in index:
                raise ValueError(f"Evidence citation is not in the supplied index: {citation}")
            if first:
                lines = len(Path(index[path]["path"]).read_text(errors="replace").splitlines())
                if not 1 <= int(first) <= int(last or first) <= lines:
                    raise ValueError(f"Evidence citation has invalid line bounds: {citation}")


def evidence_index(task: Path, directory: Path) -> dict:
    """Keep every changed submission, without repeating entire unchanged repos."""
    index = {}

    def add(prefix, root, path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            return
        key = prefix + "/" + path.relative_to(root).as_posix()
        index[key] = {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }

    for path in task.rglob("*"):
        relative = path.relative_to(task).as_posix()
        if relative in {
            "instruction.md",
            "task.toml",
            "environment/Dockerfile",
            "tests/Dockerfile",
            "tests/grade.py",
            "tests/test_driver.py",
            "tests/contract.json",
            "tests/source/test_codemidas_generated.py",
        } or relative.startswith("solution/"):
            add("task", task, path)
    for kind in ("exploit", *(f"solve-{i}" for i in range(4))):
        root = directory / kind
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            parts = path.relative_to(root).parts
            artifact_index = parts.index("artifacts") if "artifacts" in parts else -1
            if artifact_index >= 0 and parts[artifact_index + 1 : artifact_index + 2] == (
                "workspace",
            ):
                relative = Path(*parts[artifact_index + 2 :])
                original = task / "environment/source" / relative
                if not original.is_file() or path.read_bytes() != original.read_bytes():
                    add("attempts", directory, path)
                    key = "attempts/" + path.relative_to(directory).as_posix()
                    if key in index and original.is_file():
                        before = original.read_text(errors="replace").splitlines()
                        after = path.read_text(errors="replace").splitlines()
                        index[key]["changed_ranges"] = [
                            {
                                "kind": kind,
                                "starter_first_line": i + 1,
                                "starter_line_count": j - i,
                                "submitted_first_line": k + 1,
                                "submitted_line_count": m - k,
                            }
                            for kind, i, j, k, m in SequenceMatcher(
                                None, before, after, autojunk=False
                            ).get_opcodes()
                            if kind != "equal"
                        ]
                    if original.is_file():
                        add("task", task, original)
            elif path.name in {
                "result.json",
                "trial.json",
                "manifest.json",
                "trace.jsonl",
                "conclusion.txt",
            } or ("verifier" in path.parts and path.suffix in {".txt", ".json"}):
                add("attempts", directory, path)
    return index


def control_evidence(directory: Path, identity: str) -> list[dict]:
    result = []
    for agent, count, expected in (("nop", 2, 0), ("oracle", 4, 1)):
        for index in range(count):
            receipt = json.loads((directory / f"{agent}-{index}" / "trial.json").read_text())
            trial = read_trial(directory / f"{agent}-{index}" / receipt["trial_id"])
            if (
                receipt["state"] != "completed"
                or receipt["bundle_hash"] != identity
                or hashlib.sha256(trial.result.read_bytes()).hexdigest() != receipt["result_sha256"]
                or not trial.completed
                or trial.reward != expected
            ):
                raise ValueError(
                    "CodeMidas controls are missing, changed, or belong to another bundle"
                )
            result.append(
                {
                    "agent": agent,
                    "result": str(trial.result.resolve()),
                    "sha256": receipt["result_sha256"],
                    "reward": trial.reward,
                }
            )
    return result


def completed_attempt(output: Path, *, identity: str, model: str, mode: str, worker, ledger):
    """Reuse a completed, bound attempt when only the controller review changes.

    Its original runtime remains in the receipt. No new solver is dispatched and
    no cost is charged again; a new runtime is used only for new trial IDs.
    """
    receipt = json.loads((output / "trial.json").read_text())
    expected = {
        "bundle_hash": identity,
        "agent": "responses",
        "model": "openai/" + model,
        "agent_mode": mode,
        "max_turns": 18,
        "max_tokens": 4096,
        "timeout_sec": 900,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("Prior attempt has different task/model/settings")
    if receipt["state"] != "completed":
        recover_trial(worker, output, ledger=ledger)
        receipt = json.loads((output / "trial.json").read_text())
    trial = read_trial(output / receipt["trial_id"])
    if hashlib.sha256(trial.result.read_bytes()).hexdigest() != receipt["result_sha256"]:
        raise ValueError("Prior attempt evidence changed")
    return trial


def audit_task(
    *,
    task: Path,
    controls: Path,
    directory: Path,
    campaign: Path,
    worker_receipt: Path,
    wheel: Path,
    screen_attempts: int = 4,
    attempt_concurrency: int = 2,
    audit_model: str = "gpt-6-luna",
    screen_model: str = "gpt-6-sol",
    max_cost: str = "6",
    resume: bool = False,
):
    if not 2 <= screen_attempts <= 16:
        raise ValueError("Use 2-16 independent curriculum attempts")
    if type(attempt_concurrency) is not int or not 1 <= attempt_concurrency <= 4:
        raise ValueError("Run one to four independent attempts concurrently")
    identity = inspect_bundle(task)["bundle_hash"]
    controls_data = control_evidence(controls, identity)
    record = json.loads(worker_receipt.read_text())
    ledger = BudgetLedger(campaign / "budget.sqlite3")
    if record["state"] != "running" or Path(record["ledger"]).resolve() != ledger.path.resolve():
        raise ValueError("Audit needs a running worker in this campaign")
    worker = connect_worker(record["spec"]["provider"], record["worker_id"])
    prepare_docker(worker)
    runtime = runtime_python(check_runtime_wheel(wheel))
    install_runtime(worker, wheel, directory / "runtime")
    prefix = (
        "cm-audit-" + hashlib.sha256(f"{identity}:{directory.resolve()}".encode()).hexdigest()[:16]
    )
    budget = RunBudget(ledger, "trial:" + prefix, max_cost)
    policy_block = campaign / "codemidas-adversarial-policy-block.json"

    def attempt(kind, model, mode="solve"):
        output = directory / kind
        if resume and (output / "trial.json").exists():
            receipt = json.loads((output / "trial.json").read_text())
            if (
                receipt.get("state") in {"interrupted", "not_dispatched"}
                and receipt.get("trial_dispatched") is False
            ):
                receipt = abandon_undispatched_trial(worker, output, ledger=budget)
            if receipt.get("state") == "not_dispatched":
                if "/dispatch-1" in kind:
                    raise ValueError(
                        "The single undispatched replacement also needs reconciliation"
                    )
                evidence = attempt(kind + "/dispatch-1", model, mode)
                save_record(
                    output / "dispatch-recovery.json",
                    {"original_was_dispatched": False, "replacement_result": str(evidence.result)},
                )
                return evidence
            return completed_attempt(
                output, identity=identity, model=model, mode=mode, worker=worker, ledger=budget
            )
        return run_trial(
            worker,
            task,
            output,
            trial_id=prefix + "-" + kind.replace("/", "-"),
            agent="responses",
            agent_mode=mode,
            model=LLMSpec(provider="openai", model=model),
            ledger=budget,
            reservation_usd="1.25" if model == "gpt-6-sol" else "0.20",
            max_turns=18,
            max_tokens=4096,
            timeout_sec=900,
            python=runtime,
            resume=resume,
        )

    def audit_attempt(pair):
        kind, model = pair
        if (
            kind == "exploit"
            and policy_block.exists()
            and not (resume and (directory / kind / "trial.json").exists())
        ):
            return {
                "trial": kind,
                "reward": None,
                "exception": "ProviderPolicyBlocked",
                "provider_policy_blocked": True,
                "result": None,
            }, None
        mode = "exploit" if kind == "exploit" else "solve"
        evidence = attempt(kind, model, mode)
        policy_refused = False
        if evidence.exception_type == "BadRequestError":
            raw = json.loads(evidence.result.read_text())
            message = (raw.get("exception_info") or {}).get("exception_message", "")
            if "cyber_policy" in message and kind == "exploit":
                policy_refused = True
                save_record(
                    policy_block,
                    {
                        "code": "cyber_policy",
                        "action": "adversarial_checks_paused",
                        "result": str(evidence.result.resolve()),
                        "sha256": hashlib.sha256(evidence.result.read_bytes()).hexdigest(),
                        "detail": "Provider requires appropriate access before retrying this stage.",
                    },
                )
        retry_record = None
        if evidence.exception_type == "ProviderOutputError" and (
            kind != "exploit" or not policy_block.exists()
        ):
            original = {
                "trial": kind,
                "result": str(evidence.result.relative_to(directory)),
                "exception": evidence.exception_type,
            }
            evidence = attempt(kind + "/retry-1", model, mode)
            retry_record = {**original, "replacement": str(evidence.result.relative_to(directory))}
        return {
            "trial": kind,
            "reward": evidence.reward,
            "exception": evidence.exception_type,
            "result": str(evidence.result.relative_to(directory)),
            "provider_policy_blocked": policy_refused,
        }, retry_record

    # Complete the adversarial gate first; ordinary attempts have separate
    # sandboxes and receipts, so their dispatch can overlap without shared state.
    exploit = audit_attempt(("exploit", screen_model))
    with ThreadPoolExecutor(max_workers=attempt_concurrency) as pool:
        outcomes = [
            exploit,
            *pool.map(audit_attempt, [(f"solve-{i}", audit_model) for i in range(4)]),
        ]
    summaries = [summary for summary, _ in outcomes]
    audit_retries = [retry for _, retry in outcomes if retry is not None]
    # Review only the pinned task and its own attempts. No credentials or other
    # campaign files are addressable through this inspection tool.
    index = evidence_index(task, directory)

    async def read_evidence(path: str, first_line: int = 1, lines: int = 150):
        if (
            not isinstance(path, str)
            or path not in index
            or type(first_line) is not int
            or type(lines) is not int
            or not first_line >= 1
            or not 1 <= lines <= 400
        ):
            return "Choose a listed evidence path and 1-400 lines."
        record = index[path]
        source = Path(record["path"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError("Audit evidence changed during review")
        content = source.read_text(errors="replace").splitlines()
        return "\n".join(
            f"{i + 1}: {line}"
            for i, line in enumerate(content)
            if first_line - 1 <= i < first_line - 1 + lines
        )[:24000]

    async def read_evidence_batch(requests: list[dict]):
        if not isinstance(requests, list) or not 1 <= len(requests) <= 4:
            return "Read one to four evidence windows per call."
        result = []
        for request in requests:
            if (
                not isinstance(request, dict)
                or not isinstance(request.get("path"), str)
                or set(request) - {"path", "first_line", "lines"}
            ):
                return "Each window needs path and optional first_line/lines fields."
            content = await read_evidence(**request)
            limit = 21000 // len(requests)
            if len(content) > limit:
                content = content[:limit] + "\n[Truncated: request a smaller line window.]"
            result.append(request["path"] + "\n" + content)
        # Keep repeated windows of the same path, and avoid cutting JSON in half.
        return "\n\n".join(result)

    async def review():
        inputs = {
            "task": task.name,
            "bundle_hash": identity,
            "controls": controls_data,
            "attempts": summaries,
            "instruction": (task / "instruction.md").read_text(),
            "private_tests": (task / "tests/source/test_codemidas_generated.py").read_text(),
            "files": {
                path: {k: v for k, v in record.items() if k != "path"}
                for path, record in index.items()
            },
        }
        system = files(__package__).joinpath("audit.md").read_text()
        review_key = hashlib.sha256(
            json.dumps({"inputs": inputs, "system": system}, sort_keys=True).encode()
        ).hexdigest()[:12]
        root = directory / "reviews" / review_key

        async def check(value):
            validate_review(value, index, summaries)

        return await artifact_stage(
            schema=AuditReview,
            stage="rollout-review",
            inputs=inputs,
            system=system,
            prompt=json.dumps(inputs),
            root=root,
            model="openai/" + screen_model,
            runtime="openai",
            max_cost=1.25,
            max_turns=20,
            deadline=time.time() + 1200,
            budget=AuthorBudget(
                RunBudget(ledger, prefix + ":review:" + review_key, "1.25"),
                root / "costs",
                "review",
            ),
            extra_tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_evidence",
                        "description": "Read immutable task/trial evidence by its listed path.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "first_line": {"type": "integer"},
                                "lines": {"type": "integer"},
                            },
                            "required": ["path"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "read_evidence_batch",
                        "description": "Efficiently read up to four listed evidence windows at once.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "requests": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 4,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "path": {"type": "string"},
                                            "first_line": {"type": "integer"},
                                            "lines": {"type": "integer"},
                                        },
                                        "required": ["path"],
                                    },
                                },
                            },
                            "required": ["requests"],
                        },
                    },
                },
            ],
            extra_handlers={
                "read_evidence": read_evidence,
                "read_evidence_batch": read_evidence_batch,
            },
            validate=check,
        )

    reviewed = asyncio.run(review())
    validate_review(reviewed, index, summaries)
    screens = []
    screen_records = []
    if reviewed.solver_sound:

        def screen(index):
            records = []
            for retry in range(2):
                kind = f"screen-{index}" + ("-retry-1" if retry else "")
                evidence = attempt(kind, screen_model)
                records.append(
                    {"trial": kind, "reward": evidence.reward, "exception": evidence.exception_type}
                )
                # Never repeat a valid failure or an unknown transport outcome.
                if evidence.completed or evidence.exception_type != "ProviderOutputError":
                    break
            return evidence.reward if evidence.completed else None, records

        with ThreadPoolExecutor(max_workers=attempt_concurrency) as pool:
            for reward, records in pool.map(screen, range(screen_attempts)):
                screens.append(reward)
                screen_records.extend(records)
    result = {
        "bundle_hash": identity,
        "method_sound": reviewed.sound,
        "solver_review_sound": reviewed.solver_sound,
        "adversarial_status": (
            "blocked"
            if summaries[0].get("provider_policy_blocked")
            else "completed"
            if summaries[0]["exception"] is None
            else "incomplete"
        ),
        "review": reviewed.model_dump(),
        "audit_trials": summaries,
        "audit_retries": audit_retries,
        "screen_model": screen_model,
        "screen_rewards": screens,
        "screen_trials": screen_records,
        "curriculum": curriculum(screens),
        "curriculum_selected": reviewed.sound and curriculum(screens) == "mixed",
        "paper_screen_count_unspecified": True,
        "attempt_concurrency": attempt_concurrency,
        "controls": controls_data,
    }
    from repo2rlenv.pipelines.recipes.codemidas.retention import retain

    revision = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:16]
    receipt = directory / "results" / (revision + ".json")
    save_record(receipt, result)
    retained = retain(task, directory / "retained" / revision / task.name, controls, audit=receipt)
    result["retained_task"] = str(retained.resolve())
    result["audit_receipt"] = str(receipt.resolve())
    save_record(directory / "result.json", result)
    return result
