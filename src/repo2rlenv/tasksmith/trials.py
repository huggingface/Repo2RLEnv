"""Budgeted Harbor trials with trusted transfer and provider-lifecycle receipts.

``run_trial(task_path, config, budget, root, kind, *, model=None, script=None,
oracle=False, deadline=...)`` uses a fresh root exactly once. ``oracle=True``
selects Harbor's oracle, or applies gold before a supplied negative/alternative
script. Raw behavioral rewards are observations, not admission: a separately
paired source-change witness is still required by the campaign.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shlex
import stat
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.evaluate import inspect_execution, trial_name
from repo2rlenv.curation.inference import inference_digest
from repo2rlenv.curation.models import Contract
from repo2rlenv.tasksmith.bootstrap import cpu_estimate
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.emit import collection_inventory, verify_collection
from repo2rlenv.tasksmith.providers import BuildSpec
from repo2rlenv.tasksmith.worker import canonical_digest, save_json


@dataclass
class TrialOutcome:
    kind: str
    task_digest: str
    path: str
    provider: str
    profile_digest: str
    materialization_digest: str
    model: str | None = None
    inference_digest: str | None = None
    observed_reward: float | None = None
    reward: float | None = None
    error: str | None = None
    collection_verified: bool = False
    cleanup_confirmed: bool = False
    materialization_verified: bool = False
    source_observation: dict | None = None
    reservation_id: str | None = None
    cloud_cost_estimated_usd: float | None = None
    model_cost_usd: float | None = None

    def model_dump(self):
        return asdict(self)


# Trusted transport code only: run remotely as root with isolated Python. It
# never imports source, tests, sitecustomize, or a package controlled by a solver.
_INVENTORY = r"""
import os, stat, pwd, signal, json, sys, hashlib, pathlib, time
paths=json.loads(sys.argv[1]); uid=pwd.getpwnam('agent').pw_uid
assert uid != 0
for attempt in range(5):
    survivors=[]
    for entry in pathlib.Path('/proc').iterdir():
        if not entry.name.isdigit(): continue
        try:
            if entry.stat().st_uid == uid:
                state=(entry/'stat').read_text().rpartition(')')[2].split()[0]
                if state in ('Z','X'): continue
                survivors.append(int(entry.name)); os.kill(int(entry.name),signal.SIGKILL)
        except ProcessLookupError: pass
        except FileNotFoundError: pass
    if not survivors: break
    time.sleep(0.05)
else: raise RuntimeError('Agent processes remain after quiescence')
result={}; total=0; entries=0
for name in paths:
    rel=pathlib.PurePosixPath(name)
    assert str(rel)==name and not rel.is_absolute() and '..' not in rel.parts
    target=pathlib.Path('/workspace')/name
    for parent in (target,*target.parents):
        assert not parent.is_symlink(), 'linked submission parent'
    assert target.exists(), 'missing declared submission'
    for file in sorted([target] if target.is_file() else target.rglob('*')):
        s=file.lstat(); entries+=1
        assert entries<=20000, 'submission has too many entries'
        assert stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode), 'nonregular submission'
        if stat.S_ISDIR(s.st_mode): continue
        rel=file.relative_to('/workspace')
        if '.git' in rel.parts or '__pycache__' in rel.parts or file.suffix=='.pyc': continue
        assert s.st_nlink==1 and s.st_size<=10000000, 'linked or oversized submission file'
        total+=s.st_size; assert total<=100000000, 'submission exceeds 100 MB'
        h=hashlib.sha256()
        fd=os.open(file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno()); assert stat.S_ISREG(before.st_mode)
            count=0
            while chunk:=stream.read(262144):
                count+=len(chunk); assert count<=10000000
                h.update(chunk)
            after=os.fstat(stream.fileno())
        assert (before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_ino)==(after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_ino)
        assert count==s.st_size
        result[rel.as_posix()]={'sha256':h.hexdigest(),'size_bytes':count}
assert result, 'empty submission'
print(json.dumps(result,sort_keys=True))
"""


def _read_json(path: Path, limit=10_000_000):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"Missing, linked or oversized evidence: {path.name}")
    return json.loads(
        path.read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value))
    )


def _task_policy(task_path: Path):
    from harbor.models.task.config import TaskConfig

    # Complete regular-tree validation precedes all claims and provider calls.
    if any(p.is_symlink() for p in (task_path, *task_path.parents)) or not task_path.is_dir():
        raise ValueError("Task snapshot must be an unlinked directory")
    files, total = 0, 0
    for path in task_path.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Nonregular task snapshot entry")
        files += 1
        total += info.st_size
        if files > 500 or total > 20_000_000:
            raise ValueError("Task snapshot exceeds supported bounds")
    contract = Contract.model_validate(_read_json(task_path / "contract.json"))
    raw = tomllib.loads((task_path / "task.toml").read_text())
    task = TaskConfig.model_validate(raw)
    if (
        task.schema_version != "1.4"
        or task.steps
        or task.verifier.environment_mode.value != "separate"
    ):
        raise ValueError("Trials require schema1.4 single-step separate grading")
    if task.agent.user != "agent":
        raise ValueError("Protected transfer requires the unprivileged agent user")
    for env in (task.environment, task.verifier.environment):
        if env is None or env.network_mode.value != "no-network":
            raise ValueError("Both CPU trial roles must have no-network baselines")
        if env.gpus or env.tpu or env.os.value != "linux" or env.env:
            raise ValueError("Unsupported trial profile or host environment interpolation")
        if (env.cpus or 2) > 8 or (env.memory_mb or 8192) > 32768:
            raise ValueError("Task resources exceed the supported CPU profile")
    if task.solution.env or task.verifier.env:
        raise ValueError("Tasksmith trials do not hydrate host variables into task roles")
    expected = ["/workspace/" + name for name in contract.source_paths]
    if [entry.source for entry in task.artifacts] != expected:
        raise ValueError("Artifact declaration differs from contract collection")
    if any(a.destination or a.service not in {None, "main"} for a in task.artifacts):
        raise ValueError("Custom artifact destinations/services are unsupported")
    for directory in (task_path / "environment", task_path / "tests"):
        if any("compose" in p.name.lower() for p in directory.iterdir()):
            raise ValueError("Compose requires separate profile conformance")
    if task.verifier.collect:
        raise ValueError("Mutable collection hooks are unsupported")
    materialization = _read_json(task_path / "tests/materialization.json")
    if (
        materialization.get("mode") != "source_only"
        or materialization.get("source_paths") != contract.source_paths
    ):
        raise ValueError("Tasksmith source-only materialization contract missing")
    return task, contract, materialization


def _resources(task):
    return max(task.environment.cpus or 2, task.verifier.environment.cpus or 2), max(
        task.environment.memory_mb or 8192, task.verifier.environment.memory_mb or 8192
    )


def _cleanup_receipts(
    receipt_root: Path, provider: str, deadline: float, build_digests: dict[str, str]
) -> tuple[bool, list[dict]]:
    receipts = [_read_json(p) for p in sorted(receipt_root.glob("*.json"))]
    okay = bool(receipts) and all(
        r.get("provider") == provider
        and r.get("deadline") == deadline
        and r.get("status") == "stopped"
        and r.get("cleanup_confirmed") is True
        and r.get("resource_id")
        and r.get("build_digest") == build_digests.get(r.get("role"))
        for r in receipts
    )
    return bool(okay), receipts


def _inspect_collection(folder: Path, expected: dict, contract: Contract) -> tuple[str, dict]:
    manifest = _read_json(folder / "artifacts/manifest.json")
    required = {"/workspace/" + p for p in contract.source_paths}
    matched = [row for row in manifest if row.get("source") in required]
    if len(matched) != len(required) or {row["source"] for row in matched} != required:
        raise ValueError("Harbor omitted a required artifact receipt")
    for row in matched:
        if row.get("status") != "ok" or row.get("destination") != "artifacts/" + row[
            "source"
        ].lstrip("/"):
            raise ValueError("Required Harbor artifact was skipped, missing, or redirected")
    host = collection_inventory(folder / "artifacts/workspace", contract.source_paths)
    verify_collection(expected, host)
    actual = _read_json(folder / "verifier/collection.json")
    checksum = verify_collection(expected, actual)
    details = _read_json(folder / "verifier/details.json")
    if details.get("valid") is not True or details.get("collection_digest") != checksum:
        raise ValueError("Protected grading is incomplete or collection identity differs")
    origin = details.get("origin", "")
    if not origin.startswith("/workspace/") or origin[len("/workspace/") :] not in actual:
        raise ValueError("Grader source origin is outside the transferred file inventory")
    return checksum, details


async def run_trial(
    task_path: Path,
    config: TasksmithConfig,
    budget: Budget,
    root: Path,
    kind: str,
    *,
    model: str | None = None,
    script: str | None = None,
    oracle: bool = False,
    deadline: float,
) -> TrialOutcome:
    """Execute once. Retain reservations unless every created role is confirmed stopped.

    ``reward`` requires healthy execution and exact transfer. ``observed_reward``
    also preserves an untrusted/invalid Harbor report for diagnosis. Neither field
    alone asserts admission or a paired materialization witness. Infrastructure
    failures produce ``reward=None``; healthy protected-test failures produce 0.
    """
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.hooks import TrialEvent
    from harbor.trial.trial import Trial

    if not math.isfinite(deadline) or deadline - time.time() < 2:
        raise TimeoutError("Trial absolute deadline exhausted")
    if not kind or (model is not None and (oracle or script is not None)):
        raise ValueError(
            "Specify a model, oracle, or control script without mixing model and controls"
        )
    task, contract, materialization = _task_policy(task_path)
    task_digest = digest_task(task_path)
    build_digests = {
        role: BuildSpec.from_directory(task_path / directory, role=role).digest
        for role, directory in (("solver", "environment"), ("grader", "tests"))
    }
    cpus, memory = _resources(task)
    profile_digest = canonical_digest(
        {
            "provider": config.provider,
            "profile": "cpu-direct",
            "policy": 1,
            "cpus": cpus,
            "memory_mb": memory,
            "agent": task.environment.model_dump(mode="json"),
            "grader": task.verifier.environment.model_dump(mode="json"),
        }
    )
    name = trial_name(kind)
    output = root / name
    outcome = TrialOutcome(
        kind,
        task_digest,
        str(output),
        config.provider,
        profile_digest,
        canonical_digest(materialization),
        model=model,
        inference_digest=inference_digest(model, adversary=kind == "adversary") if model else None,
    )
    root.mkdir(parents=True, exist_ok=True)
    # Exclusive on-disk claim precedes reservation and all remote effects.
    with (root / "operation.json").open("x") as stream:
        json.dump(
            {
                "task_digest": task_digest,
                "config_digest": config.digest(),
                "trial": name,
                "deadline": deadline,
                "kind": kind,
                "status": "claimed",
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    seconds = min(deadline - time.time(), config.trial_timeout_sec + 2100)
    trial_deadline = min(deadline, time.time() + seconds)
    allowance = max(
        config.cloud_reservation_usd,
        cpu_estimate(config.provider, seconds + 60, cpus, memory, containers=2),
    )
    reservation = budget.reserve(allowance, f"tasksmith:trial:{kind}")
    outcome.reservation_id = reservation
    save_json(
        root / "reservation.json",
        {
            "id": reservation,
            "allowance_usd": allowance,
            "deadline": trial_deadline,
            "task_digest": task_digest,
            "status": "reserved",
        },
    )
    receipts = root / "resources"
    kwargs = {
        "budget_path": str(budget.path.resolve()),
        "budget_limit": budget.limit,
        "budget_scope": budget.scope,
        "scope_limit": budget.scope_limit,
        "budget_group": budget.group,
        "group_limit": budget.group_limit,
        "max_turns": config.solver_turns,
        "max_cost": config.solver_limit_usd,
        "mode": "adversary" if kind == "adversary" else "solve",
    }
    if oracle and script is None:
        agent = {"name": "oracle"}
    else:
        if script is not None or model is None:
            kwargs.update(mode="script", script=script or "true")
            if oracle:
                kwargs["oracle_dir"] = str((task_path / "solution").resolve())
        agent = {
            "import_path": "repo2rlenv.curation.harbor_agent:OfflineAgent",
            "model_name": model,
            "kwargs": kwargs,
        }
    agent["override_timeout_sec"] = min(config.trial_timeout_sec, seconds)
    cfg = TrialConfig.model_validate(
        {
            "task": {"path": str(task_path.resolve())},
            "trial_name": name,
            "trials_dir": str(root.resolve()),
            "agent": agent,
            "environment": {
                "type": config.provider,
                "import_path": "repo2rlenv.tasksmith.harbor_environments:Tracked"
                + config.provider.title()
                + "Environment",
                "delete": True,
                "kwargs": {
                    "receipt_root": str(receipts.resolve()),
                    "absolute_deadline": trial_deadline,
                },
                "override_cpus": cpus,
                "override_memory_mb": memory,
                "override_gpus": 0,
            },
        }
    )
    save_json(root / "harbor-config.json", cfg.model_dump(mode="json"))
    runtime = None
    expected = None
    started = time.monotonic()
    cancellation = None
    try:
        runtime = await Trial.create(cfg)

        async def capture(_event):
            nonlocal expected
            command = (
                "/usr/local/bin/python -I -c "
                + shlex.quote(_INVENTORY)
                + " "
                + shlex.quote(json.dumps(contract.source_paths))
            )
            try:
                captured = await runtime.agent_environment.exec(
                    command=command, user="root", timeout_sec=60
                )
                if captured.return_code != 0 or len((captured.stdout or "").encode()) > 10_000_000:
                    raise ValueError(
                        f"Trusted transfer inventory failed: {(captured.stderr or captured.stdout or '')[-2000:]}"
                    )
                expected = json.loads(captured.stdout)
                verify_collection(expected, expected)
                save_json(root / "pretransfer.json", expected)
            except BaseException:
                # Harbor otherwise tries best-effort recovery collection after a
                # failed hook, including the very oversized/linked tree rejected.
                runtime._are_artifacts_collected = True
                raise

        runtime.add_hook(TrialEvent.AGENT_END, capture)
        async with asyncio.timeout(max(0.001, trial_deadline - time.time())):
            result = await runtime.run()
        if result.agent_result:
            outcome.model_cost_usd = result.agent_result.cost_usd or 0
        rewards = result.verifier_result.rewards if result.verifier_result else None
        if rewards:
            raw_reward = rewards.get("reward")
            if type(raw_reward) in (int, float) and math.isfinite(raw_reward):
                outcome.observed_reward = float(raw_reward)
        if result.exception_info:
            raise RuntimeError(
                result.exception_info.exception_type
                + ": "
                + result.exception_info.exception_message
            )
        execution_error = inspect_execution(output)
        if execution_error:
            raise RuntimeError(execution_error)
        if expected is None:
            raise ValueError("Trusted pretransfer hook did not complete")
        checksum, details = _inspect_collection(output, expected, contract)
        outcome.collection_verified = True
        if (
            outcome.observed_reward not in (0, 1)
            or details.get("reward") != outcome.observed_reward
        ):
            raise ValueError("Missing or inconsistent protected binary reward")
        if details.get("outcome") != (
            "passed" if outcome.observed_reward == 1 else "submission_failure"
        ):
            raise ValueError("Protected reward and outcome differ")
        outcome.reward = outcome.observed_reward
        save_json(
            root / "collection-receipt.json",
            {
                "task_digest": task_digest,
                "inventory_digest": checksum,
                "profile_digest": profile_digest,
                "verified": True,
            },
        )
        observation = output / "verifier/source-observation.json"
        if observation.exists():
            outcome.source_observation = _read_json(observation)
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            cancellation = exc
        outcome.error = type(exc).__name__ + ": " + str(exc)
        outcome.reward = None
    finally:
        if runtime is not None:
            try:
                async with asyncio.timeout(65):
                    await asyncio.shield(runtime.agent_environment.stop(delete=True))
            except BaseException as exc:
                outcome.error = (outcome.error or "") + f"; cleanup: {type(exc).__name__}"
        try:
            outcome.cleanup_confirmed, resource_records = _cleanup_receipts(
                receipts, config.provider, trial_deadline, build_digests
            )
            if outcome.cleanup_confirmed:
                amount = cpu_estimate(
                    config.provider, time.monotonic() - started, cpus, memory, containers=2
                )
                budget.settle(reservation, amount, estimated=True)
                outcome.cloud_cost_estimated_usd = amount
                save_json(
                    root / "reservation.json",
                    {
                        "id": reservation,
                        "status": "estimated",
                        "charged_usd": amount,
                        "resources": resource_records,
                    },
                )
            else:
                outcome.error = (
                    outcome.error or ""
                ) + "; provider cleanup unconfirmed; reservation retained"
            if {r.get("role") for r in resource_records} != {"solver", "grader"}:
                outcome.error = (
                    outcome.error or ""
                ) + "; complete solver/grader lifecycle evidence missing"
            if digest_task(task_path) != task_digest:
                outcome.error = (outcome.error or "") + "; immutable task changed during execution"
        except Exception as exc:
            outcome.error = (outcome.error or "") + "; evidence settlement: " + str(exc)
        if outcome.error:
            outcome.reward = None
        save_json(root / "outcome.json", asdict(outcome))
        save_json(
            root / "operation.json",
            {
                "task_digest": task_digest,
                "config_digest": config.digest(),
                "trial": name,
                "deadline": deadline,
                "kind": kind,
                "status": "completed" if outcome.error is None else "incomplete",
                "outcome_path": str(root / "outcome.json"),
            },
        )
    if cancellation:
        raise cancellation
    return outcome
