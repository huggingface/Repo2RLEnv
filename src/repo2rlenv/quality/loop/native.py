"""Harbor trials orchestrated locally with all target execution on native Modal."""

from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from repo2rlenv.auth import resolve_llm_api_key
from repo2rlenv.execution.harbor import read_trial
from repo2rlenv.execution.lifecycle import now, save_record
from repo2rlenv.llm import completion_token_limit
from repo2rlenv.quality.loop.artifacts import parse_task, task_identity
from repo2rlenv.quality.loop.remote import RemoteTrials


def validate_native_task(task: Path):
    parsed = parse_task(task)
    config = parsed.config
    if config.verifier.environment_mode.value != "separate":
        raise ValueError("Native GPU trials require a separate private verifier")
    for environment in (config.environment, config.verifier.environment):
        if environment is None or environment.gpus not in {1, 2} or environment.gpu_types != ["L4"]:
            raise ValueError("Both learner and verifier must explicitly request one or two L4 GPUs")
        if environment.network_mode.value != "no-network":
            raise ValueError(
                "Native GPU trials require no-network learner and verifier environments"
            )
    return parsed


def model_was_not_dispatched(result: Path, allocations: Path) -> bool:
    """Prove a reservation denial happened before any learner sandbox/model run."""
    data = json.loads(result.read_text())
    claims = [json.loads(path.read_text()) for path in allocations.glob("*.json")]
    return (
        bool(claims)
        and all(claim.get("state") == "reservation_failed" for claim in claims)
        and "agent_execution" in data
        and data["agent_execution"] is None
        and "agent_result" in data
        and data["agent_result"] is None
        and (data.get("exception_info") or {}).get("exception_type") == "BudgetExceeded"
    )


def model_cost_after_verifier_denial(result: Path, allocations: Path) -> Decimal | None:
    """Retain completed solver usage even when its later verifier cannot allocate."""
    data = json.loads(result.read_text())
    execution = data.get("agent_execution") or {}
    agent = data.get("agent_result") or {}
    claims = [
        json.loads(path.read_text())
        for path in allocations.glob("*.json")
        if not path.name.endswith(".cost.json")
    ]
    denied = [claim for claim in claims if claim.get("state") == "reservation_failed"]
    if (
        (data.get("exception_info") or {}).get("exception_type") != "BudgetExceeded"
        or not execution.get("finished_at")
        or not denied
        or any("__verifier__" not in claim.get("provider_name", "") for claim in denied)
        or any(claim.get("state") not in {"terminated", "reservation_failed"} for claim in claims)
        or isinstance(agent.get("cost_usd"), bool)
    ):
        return None
    try:
        cost = Decimal(str(agent.get("cost_usd")))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


def settle_completed_agent(data: dict, receipt: Path, budget, operation: str) -> bool:
    """Close model accounting before verification when the agent finished normally."""
    agent = data.get("agent_result") or {}
    if (
        data.get("exception_info") is not None
        or not (data.get("agent_execution") or {}).get("finished_at")
        or isinstance(agent.get("cost_usd"), bool)
    ):
        return False
    try:
        cost = Decimal(str(agent.get("cost_usd")))
    except InvalidOperation:
        return False
    if not cost.is_finite() or cost < 0:
        return False
    save_record(
        receipt,
        {
            "operation_id": operation,
            "trial_name": data.get("trial_name"),
            "agent_execution": data["agent_execution"],
            "agent_result": agent,
            "accounted_model_cost_usd": str(cost),
            "verifier_pending": True,
        },
    )
    budget.settle(operation, cost, evidence=str(receipt.resolve()))
    return True


class NativeModalTrials:
    """The same trial-runner interface as RemoteTrials, without a Docker worker."""

    def __init__(self, directory, budget, options):
        self.directory, self.budget, self.options = directory, budget, options

    def identity(self):
        from importlib.metadata import version

        from repo2rlenv.execution import harbor_modal
        from repo2rlenv.quality.loop import native_recovery

        return {
            "provider": "native-modal",
            "harbor_version": version("harbor"),
            "adapter_sha256": hashlib.sha256(Path(harbor_modal.__file__).read_bytes()).hexdigest(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "recovery_sha256": hashlib.sha256(
                Path(native_recovery.__file__).read_bytes()
            ).hexdigest(),
        }

    def run(self, task: Path, role: str, key: str):
        output = self.directory / "trials" / key
        validate_native_task(task)
        if (output / "trial.json").exists():
            previous = RemoteTrials._read(output, task, role)
            if (
                role == "rollout"
                and previous.exception_type == "BudgetExceeded"
                and (output / "verifier-inputs.json").exists()
            ):
                from repo2rlenv.quality.loop.native_recovery import resume_verifier

                resumed = output / "verifier-resume"
                # One continuation per trial. A lost provider response is never retried.
                if not resumed.exists():
                    asyncio.run(
                        resume_verifier(
                            task, output, resumed, self.budget, self.options, self.identity()
                        )
                    )
                return RemoteTrials._read(resumed, task, role)
            return previous
        asyncio.run(self._run(task, role, key, output))
        return RemoteTrials._read(output, task, role)

    async def _run(self, task, role, key, output):
        from harbor.models.trial.config import TrialConfig
        from harbor.trial.hooks import TrialEvent
        from harbor.trial.single_step import SingleStepTrial
        from harbor.trial.trial import Trial

        from repo2rlenv.execution.harbor_modal import ACCOUNTING, NativeAccounting

        model = self.options.solver_model if role == "rollout" else None
        agent = {"baseline": "nop", "oracle": "oracle", "probe": "oracle", "rollout": "terminus-2"}[
            role
        ]
        if model is not None:
            if model.endpoint or model.fallback or model.provider not in {"anthropic", "openai"}:
                raise ValueError("Native blind trials require a direct model provider")
            if not resolve_llm_api_key(model.provider, model.api_key_env):
                raise ValueError("Solver credentials are missing; no native dispatch")
            if model.api_key_env is not None:
                raise ValueError(
                    "Native trials currently require the standard provider API key environment variable"
                )
        output.mkdir(parents=True, exist_ok=False)
        receipt = output / "trial.json"
        trial_id = self.budget.prefix + "-" + key
        operation = "trial:" + trial_id
        record = {
            "trial_id": trial_id,
            "bundle_hash": task_identity(task),
            "agent": agent,
            "model": model.qualified_name if model else None,
            "state": "claimed",
            "started_at": now(),
            "execution": self.identity(),
        }
        save_record(receipt, record)
        if model:
            self.budget.reserve(
                operation, self.options.solver_reservation_usd, "Blind native Harbor GPU rollout"
            )
        kwargs = (
            {
                "max_turns": self.options.max_turns,
                "record_terminal_session": False,
                "llm_call_kwargs": completion_token_limit(model, self.options.solver_tokens),
            }
            if model
            else {}
        )
        configuration = TrialConfig.model_validate(
            {
                "task": {"path": str(task.resolve())},
                "trial_name": "r2e-" + hashlib.sha256(trial_id.encode()).hexdigest()[:24],
                "trials_dir": str(output / trial_id),
                "agent": {
                    "name": agent,
                    "model_name": model.qualified_name if model else None,
                    "kwargs": kwargs,
                },
                "environment": {
                    "import_path": "repo2rlenv.execution.harbor_modal:MeteredModalEnvironment",
                    "kwargs": {
                        "app_name": "repo2rlenv-owned-generation",
                        "sandbox_timeout_secs": min(1800, self.options.trial_timeout_sec),
                    },
                },
            }
        )
        accounting = NativeAccounting(self.budget, output / "allocations")
        token = ACCOUNTING.set(accounting)
        cancelled = False
        model_settled = False

        async def before_verifier(event):
            nonlocal model_settled
            model_settled = settle_completed_agent(
                event.result.model_dump(mode="json"),
                output / "completed-agent.json",
                self.budget,
                operation,
            )
            if model_settled:
                record["model_dispatch"] = "completed_before_verification"
                save_record(receipt, record)

        try:
            trial = await Trial.create(configuration)
            # A multi-step trial may call the model again after verification.
            if model and isinstance(trial, SingleStepTrial):
                trial.add_hook(TrialEvent.VERIFICATION_START, before_verifier)
            record["state"] = "dispatched"
            save_record(receipt, record)
            await asyncio.wait_for(trial.run(), timeout=self.options.trial_timeout_sec)
            evidence = read_trial(output / trial_id)
            record.update(
                state="completed",
                finished_at=now(),
                result=str(evidence.result.resolve()),
                result_sha256=hashlib.sha256(evidence.result.read_bytes()).hexdigest(),
                reward=evidence.reward,
                exception_type=evidence.exception_type,
            )
            save_record(receipt, record)
            if model and not model_settled:
                completed_cost = model_cost_after_verifier_denial(
                    evidence.result, output / "allocations"
                )
                if model_was_not_dispatched(evidence.result, output / "allocations"):
                    record["model_dispatch"] = "not_started"
                    save_record(receipt, record)
                    self.budget.settle(operation, "0", evidence=str(receipt.resolve()))
                elif completed_cost is not None:
                    record["model_dispatch"] = "completed_before_verifier_denial"
                    record["model_cost_usd"] = str(completed_cost)
                    save_record(receipt, record)
                    self.budget.settle(operation, completed_cost, evidence=str(receipt.resolve()))
                elif evidence.completed and evidence.cost_usd is not None and evidence.cost_usd > 0:
                    self.budget.settle(
                        operation, evidence.cost_usd, evidence=str(receipt.resolve())
                    )
                else:
                    self.budget.mark_uncertain(
                        operation, f"Native model usage needs reconciliation: {receipt}"
                    )
            if (
                model
                and model_cost_after_verifier_denial(evidence.result, output / "allocations")
                is not None
            ):
                from repo2rlenv.quality.loop.native_recovery import seal_verifier_inputs

                seal_verifier_inputs(task, output, self.budget)
        except BaseException as exc:
            cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
            record.update(state="interrupted", interrupted_at=now())
            save_record(receipt, record)
            if model and not model_settled:
                self.budget.mark_uncertain(operation, f"Native rollout interrupted: {receipt}")
            raise
        finally:
            try:
                results = await asyncio.gather(
                    *(environment.stop() for environment in accounting.environments),
                    return_exceptions=True,
                )
                if any(isinstance(result, BaseException) for result in results) or any(
                    environment.record
                    and environment.record["state"] not in {"terminated", "build_failed"}
                    for environment in accounting.environments
                ):
                    record["state"] = "cleanup_uncertain"
                    save_record(receipt, record)
                    if not cancelled:
                        raise RuntimeError(
                            "Native GPU cleanup needs reconciliation; retained allocation receipts"
                        )
            finally:
                ACCOUNTING.reset(token)

    def close(self):
        # Each trial owns and closes both of its environments before returning.
        pass
