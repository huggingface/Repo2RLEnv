from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import CampaignConfig, TrialEvidence

TAMPER = r"""
mkdir -p /logs/verifier 2>/dev/null || true
echo 1 > /logs/verifier/reward.txt 2>/dev/null || true
printf 'raise SystemExit(0)\n' > /workspace/sitecustomize.py
printf '[pytest]\naddopts = --ignore=/tests\n' > /workspace/pytest.ini
printf 'def pytest_collection_modifyitems(items): items.clear()\n' > /workspace/conftest.py
mkdir -p /workspace/tests
printf '#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n' > /workspace/tests/test.sh
true
"""


async def trial(
    task: Path,
    output: Path,
    label: str,
    *,
    config: CampaignConfig,
    budget: Budget,
    model: str | None = None,
    script: str | None = None,
    adversary: bool = False,
    mutation: bool = False,
) -> TrialEvidence:
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial

    output.mkdir(parents=True, exist_ok=True)
    reservation = budget.reserve(config.cloud_trial_allowance_usd, f"cloud:trial:{label}")
    started = time.monotonic()
    kwargs = {
        "budget_path": str(budget.path.resolve()),
        "budget_limit": budget.limit,
        "budget_scope": budget.scope,
        "scope_limit": budget.scope_limit,
        "max_turns": 15 if adversary else config.solver_turns,
        "max_cost": 3 if adversary else 6,
        "mode": "adversary" if adversary else "solve",
    }
    agent = (
        {"name": "oracle"}
        if label.startswith("oracle")
        else {
            "import_path": "repo2rlenv.curation.harbor_agent:OfflineAgent",
            "model_name": model,
            "kwargs": kwargs,
        }
    )
    if script is not None:
        agent["kwargs"].update(mode="script", script=script)
        if mutation:
            agent["kwargs"]["oracle_dir"] = str((task / "solution").resolve())
    agent["override_timeout_sec"] = config.trial_timeout_sec
    trial_name = f"{label}-{time.time_ns()}"
    cfg = TrialConfig.model_validate(
        {
            "task": {"path": str(task.resolve())},
            "trial_name": trial_name,
            "trials_dir": str(output.resolve()),
            "agent": agent,
            "environment": {"type": "modal", "delete": True},
        }
    )
    evidence = TrialEvidence(
        label=label, task_digest=digest_task(task), path=str(output / trial_name), model=model
    )
    try:
        runtime = await Trial.create(cfg)
        result = await asyncio.wait_for(runtime.run(), timeout=config.trial_timeout_sec + 2100)
        evidence.error = (
            result.exception_info.exception_type + ": " + result.exception_info.exception_message
            if result.exception_info
            else None
        )
        rewards = result.verifier_result.rewards if result.verifier_result else None
        if rewards:
            evidence.reward = rewards.get("reward")
        if result.agent_result:
            evidence.cost_usd = result.agent_result.cost_usd or 0
    except Exception as exc:
        evidence.error = f"{type(exc).__name__}: {exc}"
    finally:
        # Conservative 2-container CPU/RAM allowance plus build overhead. Modal
        # invoices, rather than this elapsed-time estimate, are authoritative.
        estimate = 0.10 + (time.monotonic() - started) * 0.0001
        budget.settle(reservation, estimate, estimated=True)
    (output / f"{label}.json").write_text(evidence.model_dump_json(indent=2))
    return evidence


async def preflight(
    task: Path, output: Path, *, config: CampaignConfig, budget: Budget
) -> list[TrialEvidence]:
    results = []
    for label, script in [("baseline", "true"), ("oracle-0", None)]:
        results.append(
            await trial(task, output, label, config=config, budget=budget, script=script)
        )
        if results[-1].error:
            break
    return results


def evidence_summary(trials: list[TrialEvidence]) -> str:
    summaries = []
    for t in trials:
        item = t.model_dump()
        folder = Path(t.path)
        item["verifier_logs"] = {
            str(p.relative_to(folder)): p.read_text(errors="replace")[-14000:]
            for p in folder.rglob("*")
            if p.is_file()
            and p.name in {"pytest-output.txt", "details.json", "test-stdout.txt", "exception.txt"}
        }
        summaries.append(item)
    return json.dumps(summaries, indent=2)
