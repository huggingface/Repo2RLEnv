from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from uuid import uuid4

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.build_logs import collect_modal_build_log
from repo2rlenv.curation.inference import inference_digest
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


def pytest_tamper(source_paths: list[str]) -> str:
    """Regression control for the demonstrated in-process pytest bypass."""
    paths = repr(source_paths)
    return (
        "python - <<'PY'\nfrom pathlib import Path\n"
        f"for name in {paths}:\n"
        "    root = Path('/workspace') / name\n"
        "    targets = [root] if root.is_file() else sorted(root.rglob('__init__.py'))[:10]\n"
        "    for p in targets:\n"
        "        if p.suffix == '.py':\n"
        "            with p.open('a') as f:\n"
        "                f.write('\\nimport pytest as _r2e_pytest\\n_r2e_pytest.Function.runtest = lambda self: None\\n')\n"
        "PY\n"
    )


def trial_name(label: str) -> str:
    # Harbor appends environment/verifier suffixes; Modal caps the full name at
    # 63 characters. Keep the evidence label intact, shorten only runtime IDs.
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", label)[:24]
    return f"{safe}-{uuid4().hex[:12]}"


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
    name = trial_name(label)
    cfg = TrialConfig.model_validate(
        {
            "task": {"path": str(task.resolve())},
            "trial_name": name,
            "trials_dir": str(output.resolve()),
            "agent": agent,
            "environment": {"type": "modal", "delete": True},
        }
    )
    evidence = TrialEvidence(
        label=label,
        task_digest=digest_task(task),
        path=str(output / name),
        model=model,
        inference_digest=inference_digest(model, adversary=adversary) if model else None,
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
    try:
        execution_error = inspect_execution(Path(evidence.path))
    except Exception as exc:
        execution_error = f"Execution inspection failed ({type(exc).__name__}): {exc}"
    if execution_error:
        evidence.error = (
            evidence.error + "\nSecondary inspection: " + execution_error
            if evidence.error
            else execution_error
        )
    if evidence.error:
        try:
            await collect_modal_build_log(evidence.error, Path(evidence.path))
        except Exception as exc:
            # Failed retrieval/persistence must not replace the actual build or
            # runtime failure. The exception type is sufficient here; any CLI
            # output belongs in the bounded, redacted build log.
            evidence.error += f"\nBuild log retrieval unavailable ({type(exc).__name__})."
    (output / f"{label}.json").write_text(evidence.model_dump_json(indent=2))
    return evidence


def inspect_execution(folder: Path) -> str | None:
    """Harbor's best-effort exports and oracle exit codes need explicit gates."""
    trace = folder / "agent/trace.jsonl"
    if trace.exists():
        try:
            models = [
                record
                for line in trace.read_text().splitlines()
                if (record := json.loads(line)).get("kind") == "model"
            ]
            if models:
                last = models[-1]
                message = last.get("message", {})
                if last.get("finish_reason") in {"length", "max_tokens"}:
                    return "Incomplete model response: output token limit reached"
                if not message.get("tool_calls") and not (message.get("content") or "").strip():
                    return "Incomplete model response: no final text or tool call"
        except (ValueError, TypeError, AttributeError):
            return "Malformed agent execution trace"
    exit_code = folder / "agent/exit-code.txt"
    if exit_code.exists() and exit_code.read_text().strip() != "0":
        return "Oracle execution failed with exit code " + exit_code.read_text().strip()
    manifest = folder / "artifacts/manifest.json"
    if manifest.exists():
        failed = [
            item["source"]
            for item in json.loads(manifest.read_text())
            if item.get("status") != "ok" and item.get("source", "").startswith("/workspace/")
        ]
        if failed:
            return "Submission export failed: " + ", ".join(failed)
    return None


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
            and p.name
            in {
                "pytest-output.txt",
                "details.json",
                "test-stdout.txt",
                "exception.txt",
                "oracle.txt",
                "exit-code.txt",
                "control.json",
                "manifest.json",
                "build.log",
            }
        }
        summaries.append(item)
    return json.dumps(summaries, indent=2)
