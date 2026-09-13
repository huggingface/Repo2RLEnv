"""Harbor trials orchestrated locally with all target execution on native Modal."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from repo2rlenv.auth import resolve_llm_api_key
from repo2rlenv.execution.harbor import read_trial
from repo2rlenv.execution.lifecycle import now, save_record
from repo2rlenv.llm import completion_token_limit
from repo2rlenv.quality.loop.artifacts import parse_task, task_identity
from repo2rlenv.quality.loop.remote import RemoteTrials


class NativeModalTrials:
    """The same trial-runner interface as RemoteTrials, without a Docker worker."""

    def __init__(self, directory, budget, options):
        self.directory, self.budget, self.options = directory, budget, options

    def identity(self):
        from importlib.metadata import version

        from repo2rlenv.execution import harbor_modal

        return {
            "provider": "native-modal",
            "harbor_version": version("harbor"),
            "adapter_sha256": hashlib.sha256(Path(harbor_modal.__file__).read_bytes()).hexdigest(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }

    def run(self, task: Path, role: str, key: str):
        output = self.directory / "trials" / key
        if (output / "trial.json").exists():
            return RemoteTrials._read(output, task, role)
        config = parse_task(task).config
        if config.verifier.environment_mode.value != "separate":
            raise ValueError("Native GPU trials require a separate private verifier")
        for environment in (config.environment, config.verifier.environment):
            if (
                environment is None
                or environment.gpus not in {1, 2}
                or environment.gpu_types != ["L4"]
            ):
                raise ValueError(
                    "Both learner and verifier must explicitly request one or two L4 GPUs"
                )
        asyncio.run(self._run(task, role, key, output))
        return RemoteTrials._read(output, task, role)

    async def _run(self, task, role, key, output):
        from harbor.models.trial.config import TrialConfig
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
        try:
            trial = await Trial.create(configuration)
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
            if model:
                if evidence.completed and evidence.cost_usd is not None and evidence.cost_usd > 0:
                    self.budget.settle(
                        operation, evidence.cost_usd, evidence=str(receipt.resolve())
                    )
                else:
                    self.budget.mark_uncertain(
                        operation, f"Native model usage needs reconciliation: {receipt}"
                    )
        except BaseException as exc:
            cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
            record.update(state="interrupted", interrupted_at=now())
            save_record(receipt, record)
            if model:
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
