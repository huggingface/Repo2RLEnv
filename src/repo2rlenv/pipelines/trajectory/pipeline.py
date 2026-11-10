"""Agent trajectories -> verified Harbor tasks.

Each family (repair, build, extend, qa) mines candidates from a trajectory replayed over a
virtual filesystem, builds the environment image, and keeps a task only when its trial matrix
passes in Docker (offline): the reference solution scores full reward, degenerate attempts 0.
`report.json` in the output folder records why every candidate was kept or dropped.

The one adapted file is `assets/grader.py`, from SmolDataEnvs (MIT); see its own
acknowledgment and THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import ClassVar

import tomli_w

from repo2rlenv.emitter.evaluation import generated_evaluation
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.trajectory import docker, llm
from repo2rlenv.pipelines.trajectory.adapters import convert
from repo2rlenv.pipelines.trajectory.families import FAMILIES
from repo2rlenv.pipelines.trajectory.gate import scan_trajectory
from repo2rlenv.pipelines.trajectory.task import make_tasks
from repo2rlenv.pipelines.trajectory.trajectory import Trajectory
from repo2rlenv.spec.input import PipelineName, TrajectorySource
from repo2rlenv.ui import console


def find_trajectories(root: Path) -> list[Path]:
    """ATIF .json files at root (a file, or a directory searched recursively)."""
    found = []
    for path in sorted(root.rglob("*.json")) if root.is_dir() else [root]:
        try:
            if str(json.loads(path.read_text()).get("schema_version", "")).startswith("ATIF"):
                found.append(path)
        except (ValueError, UnicodeDecodeError, AttributeError):
            pass
    return found


def convert_sessions(root: Path, fmt: str, out_dir: Path, limit: int | None) -> list[Path]:
    """Convert raw session .jsonl logs (a file, or a directory searched recursively) to ATIF files."""
    sessions = sorted(root.rglob("*.jsonl")) if root.is_dir() else [root]
    converted = []
    for session in sessions[:limit]:
        identity = hashlib.sha256(str(session.resolve()).encode()).hexdigest()[:16]
        destination = out_dir / f"{session.stem}-{identity}.json"
        try:
            trajectory = convert(str(session), fmt, str(session.resolve()))
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            # Retain a per-input failure for the same reporting boundary as ATIF inputs.
            trajectory = {"conversion_error": f"{type(error).__name__}: {error}"[:500]}
        destination.write_text(json.dumps(trajectory, indent=1, ensure_ascii=False))
        converted.append(destination)
    return converted


def add_provenance(task_dir: Path, record: dict) -> None:
    """Add the top-level version and [metadata.repo2env] table repo2rlenv tooling reads."""
    toml_path = task_dir / "task.toml"
    data = {"version": "1.0", **tomllib.loads(toml_path.read_text())}
    data.setdefault("metadata", {})["repo2env"] = {
        "spec_version": "0.2.0",
        "pipeline": PipelineName.TRAJECTORY_TO_ENV.value,
        # solution/solve.sh restores the reference files directly, so there is no patch.diff
        "recipe": "trajectory_replay",
        "reward_kinds": ["test_execution"],
        "reproducibility": {"mode": "inline_dockerfile"},
        "family": record["family"],
        "source_trace": str(record["source_trace"]),
        "evaluation": generated_evaluation(provenance="unattended"),
    }
    toml_path.write_text(tomli_w.dumps(data))


class TrajectoryPipeline:
    name: ClassVar[PipelineName] = PipelineName.TRAJECTORY_TO_ENV
    requires_bootstrap: ClassVar[bool] = False
    supported_languages: ClassVar[None] = None
    experimental: ClassVar[bool] = True

    def __init__(self, input, options, bootstrap=None):
        if not isinstance(input.source, TrajectorySource):
            raise ValueError("trajectory_to_env requires a `source: {kind: trajectory, path: ...}`")
        self.input, self.options = input, options
        llm.configure(
            input.llm,
            max_tokens=options.max_llm_tokens,
            reasoning_effort=options.reasoning_effort,
            cache_dir=options.llm_cache_dir,
            budget_usd=options.max_llm_spend_usd,
        )
        docker.PLATFORM, docker.CONTAINER_MEMORY = options.platform, options.container_memory

    def run(self, out_dir: Path) -> PipelineResult:
        source, options = self.input.source, self.options
        root = source.path.expanduser()
        with tempfile.TemporaryDirectory(prefix="trajectories-") as converted_dir:
            if source.format == "atif":
                paths = find_trajectories(root)[: options.limit]
            else:
                paths = convert_sessions(root, source.format, Path(converted_dir), options.limit)
            console.info(
                f"{len(paths)} trajectories, families: {', '.join(options.families)}, LLM: {llm.MODEL}"
            )
            records = self._process(paths, out_dir)
        if llm.SPEC is not None:
            budget = options.max_llm_spend_usd
            limit = f" of ${budget:.2f}" if budget is not None else " (no cap)"
            console.info(f"LLM spend: ${llm.spent_usd():.2f}{limit}; cached replies are free")

        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(json.dumps(records, indent=1, default=str))
        kept = [record for record in records if record["kept"]]
        for record in kept:
            add_provenance(out_dir / record["task_id"], record)
        skip_reasons = collections.Counter(r["reason"] for r in records if not r["kept"])
        return PipelineResult(
            candidates=len(records),
            emitted=len(kept),
            skipped=len(records) - len(kept),
            out_dir=out_dir,
            skip_reasons=dict(skip_reasons),
        )

    def _process(self, paths: list[Path], out_dir: Path) -> list[dict]:
        options = self.options
        seen_task_ids = {name: set() for name in options.families}

        def process(path: Path) -> list[dict]:
            try:
                return process_one(path)
            except Exception as error:
                return [
                    {
                        "trajectory": str(path),
                        "family": "-",
                        "kept": False,
                        "reason": "input_error",
                        "detail": f"{type(error).__name__}: {error}"[:500],
                    }
                ]

        def process_one(path: Path) -> list[dict]:
            passed_gate, gate_result = scan_trajectory(path) if options.scan else (True, "skipped")
            if not passed_gate:
                return [
                    {
                        "trajectory": str(path),
                        "family": "-",
                        "kept": False,
                        "reason": "gate",
                        "detail": gate_result,
                    }
                ]
            trajectory, records = Trajectory.load(path), []
            for name in options.families:
                try:
                    for record in make_tasks(
                        FAMILIES[name],
                        trajectory,
                        out_dir,
                        seen_task_ids[name],
                        options.propose_only,
                    ):
                        records.append({"trajectory": str(path), "gate": gate_result, **record})
                        status = "kept" if record["kept"] else "drop"
                        console.dim(f"{status}  {name:<7} {record['reason']:<28} {path.name}")
                except Exception as error:
                    # qa asks the model inside find_candidates, before make_tasks' own handling
                    records.append(
                        {
                            "trajectory": str(path),
                            "gate": gate_result,
                            "family": name,
                            "source_trace": trajectory.source,
                            "kept": False,
                            "reason": "budget_exhausted"
                            if isinstance(error, llm.BudgetExceeded)
                            else "error",
                            "detail": f"{type(error).__name__}: {error}"[:500],
                        }
                    )
            return records

        with concurrent.futures.ThreadPoolExecutor(options.jobs) as pool:
            records = [record for batch in pool.map(process, paths) for record in batch]
        shutil.rmtree(out_dir / ".scratch", ignore_errors=True)
        return records
