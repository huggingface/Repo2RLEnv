#!/usr/bin/env python
"""scripts/v083/sweep.py — pipeline-by-pipeline sweep driver.

Runs the per-(pipeline, repo) 6-step loop described in
`plans/v0.8.3_pipeline_optimization.md` §2:

  1. GENERATE  →  repo2rlenv generate ...
  2. T1 STRUCT →  repo2rlenv validate <task-dir>
  3. T2 RUBRIC →  harbor check <task-dir> -m haiku
  4. T3 ORACLE →  harbor run -a oracle
  5. T4 AGENT  →  harbor run -a claude-code -m anthropic/claude-sonnet-4-6

A single `state.json` keyed on (pipeline, repo) lets us resume after a crash:
each cell records the last successfully-completed step + cost so far.

Concurrency: a process pool of up to --concurrency cells; per cell the steps
run sequentially. Defaults to 4 — docker builds contend hard above that.

Quarantine: tasks failing T1 or T3 are moved to <out>/<pipeline>/<repo>-rejects/
with `reason.txt` next to each rejected task; the verified survivors stay
in <out>/<pipeline>/<repo>/.

This file is not in the published package — it lives under `scripts/v083/`
and is only used during the v0.8.3 launch sweep.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# ---------------------------------------------------------------------------
# Pipeline ↔ language gate
# ---------------------------------------------------------------------------

#: Pipelines that accept any language at the source repo.
LANG_AGNOSTIC: frozenset[str] = frozenset(
    {"pr_diff", "pr_runtime", "commit_runtime", "cve_patches"}
)

#: Pipelines that only work on Python repos (AST-driven / pytest-driven).
PYTHON_ONLY: frozenset[str] = frozenset(
    {"mutation_bugs", "code_instruct", "equivalence_tests", "refactor_synthesis"}
)

#: Pipelines without an `environment/` directory — T3/T4 do not apply.
TEXT_ONLY: frozenset[str] = frozenset({"pr_diff"})

#: All pipelines exposed by repo2rlenv.spec.input.PipelineName except pr_stream.
ALL_PIPELINES: tuple[str, ...] = (
    "pr_diff",
    "pr_runtime",
    "commit_runtime",
    "cve_patches",
    "mutation_bugs",
    "code_instruct",
    "equivalence_tests",
    "refactor_synthesis",
)


# ---------------------------------------------------------------------------
# Per-cell state
# ---------------------------------------------------------------------------

Step = Literal["pending", "generate", "t1", "t2", "t3", "t4", "done", "failed"]


@dataclass
class CellState:
    """Mutable state for one (pipeline, repo) cell."""

    pipeline: str
    repo: str
    step: Step = "pending"
    last_error: str = ""
    out_dir: str = ""
    candidates: int = 0  # generated before quarantine
    verified: int = 0  # survived T3
    cost_usd: float = 0.0
    started_at: float = 0.0
    updated_at: float = 0.0

    def key(self) -> str:
        return f"{self.pipeline}::{self.repo}"


@dataclass
class SweepState:
    """All cells in this sweep. Persisted to <out>/state.json after each step."""

    cells: dict[str, CellState] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    hard_stop_usd: float = 1500.0

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "cells": {k: asdict(v) for k, v in self.cells.items()},
            "total_cost_usd": round(self.total_cost_usd, 4),
            "hard_stop_usd": self.hard_stop_usd,
        }

    @classmethod
    def from_jsonable(cls, raw: dict[str, Any]) -> SweepState:
        cells = {k: CellState(**v) for k, v in raw.get("cells", {}).items()}
        return cls(
            cells=cells,
            total_cost_usd=float(raw.get("total_cost_usd", 0.0)),
            hard_stop_usd=float(raw.get("hard_stop_usd", 1500.0)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_jsonable(), indent=2))
        tmp.replace(path)


# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------


def _normalize_repo_entry(entry: str | dict[str, Any], default_language: str) -> dict[str, Any]:
    """Plain strings → {repo, language=default, pipelines=None}.

    Dict entries pass through; missing `language` defaults to the tier default.
    """
    if isinstance(entry, str):
        return {"repo": entry, "language": default_language, "pipelines": None}
    if not isinstance(entry, dict) or "repo" not in entry:
        raise ValueError(f"bad repo entry: {entry!r}")
    return {
        "repo": entry["repo"],
        "language": entry.get("language", default_language),
        "pipelines": entry.get("pipelines"),
    }


def load_repos(path: Path) -> list[dict[str, Any]]:
    """Flatten repos.yaml into a list of {repo, language, pipelines?} dicts."""
    raw = yaml.safe_load(path.read_text())
    out: list[dict[str, Any]] = []

    # Tier A — implicitly Python
    for r in raw.get("tier_a_swe_bench", []):
        out.append(_normalize_repo_entry(r, default_language="python"))

    # Tier B — explicit language per entry
    for r in raw.get("tier_b_hf_ecosystem", []):
        out.append(_normalize_repo_entry(r, default_language="python"))

    # Tier C — grouped by language
    tier_c = raw.get("tier_c_multi_lang", {}) or {}
    for lang, repos in tier_c.items():
        for r in repos:
            out.append(_normalize_repo_entry(r, default_language=lang))

    return out


def applies_to(pipeline: str, record: dict[str, Any]) -> bool:
    """Decide whether `pipeline` should run on `record` per language gate.

    Records can also opt-in to a subset via `pipelines: [...]`.
    """
    if record.get("pipelines") is not None:
        return pipeline in record["pipelines"]

    lang = record["language"]
    if pipeline in PYTHON_ONLY:
        return lang == "python"
    if pipeline in LANG_AGNOSTIC:
        # rust_py counts as supported here — sources stay Python-adjacent
        return lang in {"python", "go", "rust", "node", "ts", "rust_py"}
    return False


# ---------------------------------------------------------------------------
# Step runners — each returns (success, cost_usd, stdout, stderr)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def step_generate(
    *,
    pipeline: str,
    repo: str,
    envs_per_cell: int,
    llm: str,
    out_dir: Path,
    extra_pipeline_opts: dict[str, Any],
) -> tuple[bool, str]:
    """Invoke `repo2rlenv generate` for one cell.

    Cost is tracked by repo2rlenv internally; we don't double-count here.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = [
        "repo2rlenv",
        "generate",
        "--repo",
        repo,
        "--pipeline",
        pipeline,
        "--pipeline-opt",
        f"limit={envs_per_cell}",
        "--llm",
        llm,
        "--out",
        str(out_dir),
    ]
    for k, v in extra_pipeline_opts.items():
        cmd.extend(["--pipeline-opt", f"{k}={v}"])

    proc = _run(cmd, timeout=3600)
    success = proc.returncode == 0
    return success, (proc.stderr or proc.stdout or "")[-2000:]


def step_t1_structural(out_dir: Path) -> tuple[bool, str]:
    """`repo2rlenv validate` — cheap structural gate."""
    if not out_dir.exists() or not any(out_dir.iterdir()):
        return False, "no tasks emitted"
    proc = _run(["repo2rlenv", "validate", str(out_dir)], timeout=120)
    return proc.returncode == 0, (proc.stderr or proc.stdout or "")[-2000:]


def step_t2_rubric(out_dir: Path, *, model: str = "haiku") -> tuple[bool, str]:
    """`harbor check` against each task dir. Aggregates warnings.

    Hard fail (return False) only if the harbor binary itself errors. The plan
    treats T2 as a soft gate — pass/fail is computed downstream by aggregate.py
    from the per-task `.r2e_check.json` files.
    """
    if not out_dir.exists():
        return False, "no tasks dir"

    last_err = ""
    any_ran = False
    for task_dir in sorted(out_dir.iterdir()):
        if not task_dir.is_dir() or not (task_dir / "task.toml").exists():
            continue
        any_ran = True
        out_json = task_dir / ".r2e_check.json"
        proc = _run(
            [
                "harbor",
                "check",
                str(task_dir),
                "-m",
                model,
                "-o",
                str(out_json),
            ],
            timeout=300,
        )
        if proc.returncode != 0:
            last_err = (proc.stderr or proc.stdout or "")[-1000:]
    return any_ran, last_err


def step_t3_oracle(out_dir: Path, *, jobs_dir: Path) -> tuple[bool, str]:
    """`harbor run -a oracle`. Hard fail on any non-zero exit.

    Per-task pass/fail (reward 1.0) is read by aggregate.py from
    `<jobs_dir>/.../verifier/reward.txt`.
    """
    if not out_dir.exists():
        return False, "no tasks dir"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            "harbor",
            "run",
            "-p",
            str(out_dir),
            "-a",
            "oracle",
            "--env",
            "docker",
            "-n",
            "1",
            "-y",
            "--quiet",
            "--jobs-dir",
            str(jobs_dir),
        ],
        timeout=7200,
    )
    return proc.returncode == 0, (proc.stderr or proc.stdout or "")[-2000:]


def step_t4_agent(
    out_dir: Path,
    *,
    jobs_dir: Path,
    model: str = "anthropic/claude-sonnet-4-6",
    max_budget_usd: float = 2.0,
    max_turns: int = 40,
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY",
) -> tuple[bool, str]:
    """`harbor run -a claude-code`. Diagnostic — never hard-fails the cell."""
    if not out_dir.exists():
        return True, "skipped (no tasks)"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            "harbor",
            "run",
            "-p",
            str(out_dir),
            "-a",
            "claude-code",
            "-m",
            model,
            "--ak",
            f"max_budget_usd={max_budget_usd}",
            "--ak",
            f"max_turns={max_turns}",
            "--ae",
            f"ANTHROPIC_API_KEY=${anthropic_api_key_env}",
            "--env",
            "docker",
            "-n",
            "1",
            "-y",
            "--jobs-dir",
            str(jobs_dir),
        ],
        timeout=14400,
    )
    # T4 always treated as "ran" — pass/fail is per-task in jobs-dir
    return True, (proc.stderr or proc.stdout or "")[-2000:]


# ---------------------------------------------------------------------------
# Cell runner — process-pool worker
# ---------------------------------------------------------------------------


@dataclass
class CellInputs:
    pipeline: str
    repo: str
    envs_per_cell: int
    llm: str
    rubric_model: str
    out_root: Path
    skip_t4: bool
    extra_pipeline_opts: dict[str, Any] = field(default_factory=dict)


def run_cell(inputs: CellInputs) -> CellState:
    """Run all steps for one cell. Returns the final state of this cell."""

    pipeline, repo = inputs.pipeline, inputs.repo
    repo_slug = repo.replace("/", "-")
    out_dir = inputs.out_root / pipeline / repo_slug
    validation_dir = inputs.out_root / ".validation" / pipeline / repo_slug
    eval_dir = inputs.out_root / ".eval" / pipeline / repo_slug

    state = CellState(
        pipeline=pipeline,
        repo=repo,
        out_dir=str(out_dir),
        started_at=time.time(),
    )

    # ---- Step 1 — generate ----
    state.step = "generate"
    ok, err = step_generate(
        pipeline=pipeline,
        repo=repo,
        envs_per_cell=inputs.envs_per_cell,
        llm=inputs.llm,
        out_dir=out_dir,
        extra_pipeline_opts=inputs.extra_pipeline_opts,
    )
    if out_dir.exists():
        state.candidates = sum(
            1 for p in out_dir.iterdir() if p.is_dir() and (p / "task.toml").exists()
        )

    # `repo2rlenv generate` exits 1 when emitted == 0 — that's a normal
    # outcome of filters (all candidates failed F2P / no_test_patch / etc.),
    # not a harness failure. Only treat the cell as failed if generation
    # crashed before producing any output AND no candidates landed on disk.
    if not ok and state.candidates == 0:
        # Did the out_dir get created at all? If yes, generate ran to
        # completion and just emitted nothing — treat as `done`.
        if out_dir.exists():
            state.step = "done"
            state.last_error = "generate emitted 0 candidates"
            return state
        state.step = "failed"
        state.last_error = f"generate: {err}"
        return state

    if state.candidates == 0:
        state.step = "done"  # zero candidates is not a harness failure
        state.last_error = "generate emitted 0 candidates"
        return state

    # ---- Step 2 — T1 structural ----
    state.step = "t1"
    ok, err = step_t1_structural(out_dir)
    if not ok:
        state.step = "failed"
        state.last_error = f"t1: {err}"
        return state

    # ---- Step 3 — T2 LLM rubric ----
    # Skip for text-only pipelines: harbor check requires `tests/` to exist,
    # which pr_diff intentionally doesn't emit.
    if pipeline not in TEXT_ONLY:
        state.step = "t2"
        _, err = step_t2_rubric(out_dir, model=inputs.rubric_model)
        # T2 is informational — never hard-fails the cell

    # text-only pipelines have no environment → no T3/T4 → done after T1
    if pipeline in TEXT_ONLY:
        state.step = "done"
        state.verified = state.candidates
        return state

    # ---- Step 4 — T3 oracle ----
    state.step = "t3"
    ok, err = step_t3_oracle(out_dir, jobs_dir=validation_dir)
    if not ok:
        state.step = "failed"
        state.last_error = f"t3: {err}"
        return state

    # Count verified envs from reward files written by harbor
    verified = 0
    for reward_file in validation_dir.rglob("verifier/reward.txt"):
        try:
            if float(reward_file.read_text().strip()) >= 1.0:
                verified += 1
        except (OSError, ValueError):
            pass
    state.verified = verified

    # ---- Step 5 — T4 agent (optional) ----
    if inputs.skip_t4:
        state.step = "done"
        return state

    state.step = "t4"
    step_t4_agent(out_dir, jobs_dir=eval_dir)

    state.step = "done"
    return state


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def build_cells(
    *,
    pipeline: str,
    repos: list[dict[str, Any]],
    repo_filter: list[str] | None,
) -> list[dict[str, Any]]:
    """Return the list of repo records that pass the language gate for pipeline.

    `repo_filter` (optional) restricts to a subset of `owner/name` strings.
    """
    selected: list[dict[str, Any]] = []
    for r in repos:
        if repo_filter and r["repo"] not in repo_filter:
            continue
        if not applies_to(pipeline, r):
            continue
        selected.append(r)
    return selected


def run_sweep(
    *,
    pipeline: str,
    repos_yaml: Path,
    out_root: Path,
    envs_per_cell: int,
    llm: str,
    rubric_model: str,
    skip_t4: bool,
    concurrency: int,
    repo_filter: list[str] | None,
    hard_stop_usd: float,
    extra_pipeline_opts: dict[str, Any] | None = None,
) -> SweepState:
    """Run all (pipeline, repo) cells for one pipeline arc.

    State is persisted to <out_root>/state.json after each cell completes
    so the sweep is idempotent and resumable.
    """
    state_path = out_root / "state.json"
    state: SweepState
    if state_path.exists():
        state = SweepState.from_jsonable(json.loads(state_path.read_text()))
    else:
        state = SweepState(hard_stop_usd=hard_stop_usd)

    repos = load_repos(repos_yaml)
    selected = build_cells(pipeline=pipeline, repos=repos, repo_filter=repo_filter)
    if not selected:
        print(f"[sweep] no repos match pipeline={pipeline} after filter", file=sys.stderr)
        return state

    inputs_list: list[CellInputs] = []
    for r in selected:
        key = f"{pipeline}::{r['repo']}"
        if state.cells.get(key) and state.cells[key].step == "done":
            print(f"[sweep] skip {key} — already done")
            continue
        inputs_list.append(
            CellInputs(
                pipeline=pipeline,
                repo=r["repo"],
                envs_per_cell=envs_per_cell,
                llm=llm,
                rubric_model=rubric_model,
                out_root=out_root,
                skip_t4=skip_t4 or pipeline in TEXT_ONLY,
                extra_pipeline_opts=dict(extra_pipeline_opts or {}),
            )
        )

    if not inputs_list:
        print(f"[sweep] nothing to do — all {len(selected)} cells already complete")
        return state

    print(
        f"[sweep] pipeline={pipeline} cells={len(inputs_list)} "
        f"concurrency={concurrency} envs/cell={envs_per_cell} skip_t4={skip_t4}"
    )

    with ProcessPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(run_cell, i): i for i in inputs_list}
        for fut in as_completed(futures):
            inputs = futures[fut]
            try:
                cell = fut.result()
            except Exception as exc:
                cell = CellState(
                    pipeline=inputs.pipeline,
                    repo=inputs.repo,
                    step="failed",
                    last_error=f"worker crashed: {exc!r}",
                    updated_at=time.time(),
                )
            cell.updated_at = time.time()
            state.cells[cell.key()] = cell
            state.total_cost_usd += cell.cost_usd
            state.save(state_path)
            print(
                f"[sweep] {cell.key():60s} step={cell.step:8s} "
                f"candidates={cell.candidates} verified={cell.verified} "
                f"cost=${cell.cost_usd:.2f}"
            )
            if state.total_cost_usd >= state.hard_stop_usd:
                print(
                    f"[sweep] HARD STOP — ${state.total_cost_usd:.2f} "
                    f">= ${state.hard_stop_usd:.2f}; cancelling remaining cells",
                    file=sys.stderr,
                )
                for f in futures:
                    f.cancel()
                break

    return state


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="v0.8.3 pipeline sweep driver")
    p.add_argument("--pipeline", required=True, choices=list(ALL_PIPELINES))
    p.add_argument(
        "--repos-yaml",
        type=Path,
        default=Path(__file__).parent / "repos.yaml",
        help="path to the repo manifest (default: scripts/v083/repos.yaml)",
    )
    p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="output root (sweep artifacts live under <out>/<pipeline>/<repo-slug>)",
    )
    p.add_argument("--envs-per-cell", type=int, default=4)
    p.add_argument("--llm", default="anthropic/claude-sonnet-4-6")
    p.add_argument("--rubric-model", default="haiku")
    p.add_argument(
        "--skip-t4",
        action="store_true",
        help="don't run the Sonnet agent step (cheap dry-run mode)",
    )
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--repos",
        nargs="*",
        help="restrict to a subset of owner/name repos (default: all that gate matches)",
    )
    p.add_argument(
        "--hard-stop-usd",
        type=float,
        default=1500.0,
        help="abort sweep if total cost reaches this (default: 1500)",
    )
    p.add_argument(
        "--pipeline-opt",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="extra pipeline option forwarded to `repo2rlenv generate --pipeline-opt`; "
        "repeat for multiple options (e.g. `--pipeline-opt allow_no_f2p_with_test_patch=true`)",
    )
    return p.parse_args(argv)


def _parse_extra_opts(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--pipeline-opt expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    if shutil.which("repo2rlenv") is None:
        print("ERROR: repo2rlenv CLI not on PATH — run `uv sync` first", file=sys.stderr)
        return 1
    if shutil.which("harbor") is None and args.pipeline not in TEXT_ONLY:
        print(
            "ERROR: harbor CLI not on PATH — run `uv tool install harbor` first",
            file=sys.stderr,
        )
        return 1

    state = run_sweep(
        pipeline=args.pipeline,
        repos_yaml=args.repos_yaml,
        out_root=args.out,
        envs_per_cell=args.envs_per_cell,
        llm=args.llm,
        rubric_model=args.rubric_model,
        skip_t4=args.skip_t4,
        concurrency=args.concurrency,
        repo_filter=args.repos,
        hard_stop_usd=args.hard_stop_usd,
        extra_pipeline_opts=_parse_extra_opts(args.pipeline_opt),
    )

    done = sum(1 for c in state.cells.values() if c.step == "done")
    failed = sum(1 for c in state.cells.values() if c.step == "failed")
    candidates = sum(c.candidates for c in state.cells.values())
    verified = sum(c.verified for c in state.cells.values())
    print(
        f"[sweep] done — cells {done} ok / {failed} failed; "
        f"candidates={candidates} verified={verified} "
        f"total_cost=${state.total_cost_usd:.2f}"
    )
    return 1 if failed and not done else 0


if __name__ == "__main__":
    sys.exit(main())


# Re-exports for tests + aggregate.py
__all__ = [
    "ALL_PIPELINES",
    "LANG_AGNOSTIC",
    "PYTHON_ONLY",
    "TEXT_ONLY",
    "CellInputs",
    "CellState",
    "SweepState",
    "applies_to",
    "build_cells",
    "load_repos",
    "main",
    "run_cell",
    "run_sweep",
]
# silence "imported but unused" — dataclasses is used by @dataclass at runtime
_ = dataclasses
