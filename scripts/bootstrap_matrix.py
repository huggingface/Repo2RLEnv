"""Async orchestrator for the v0.8.1 bootstrap robustness matrix.

Computes (repo, model) cells from a YAML manifest, runs bootstraps under
bounded concurrency (Docker Desktop on Mac caps useful parallelism around
4), and incrementally updates a JSON state file + the human-readable
markdown results sheet as each cell finishes.

Resumable: cells with state "success" in the JSON file are skipped on
re-run. To re-attempt a cell, pass `--rerun <owner>/<repo>:<model_label>`
(can be repeated) or `--rerun-failed` to retry every non-success row.

Usage:
    uv run python scripts/bootstrap_matrix.py                    # run all pending
    uv run python scripts/bootstrap_matrix.py --tier "Tier 0"    # one tier
    uv run python scripts/bootstrap_matrix.py --rerun-failed     # retry 🔴/🟡
    uv run python scripts/bootstrap_matrix.py --dry-run          # print plan only
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Make the local package importable when running as a plain script.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from repo2rlenv.bootstrap import ensure_bootstrap  # noqa: E402
from repo2rlenv.bootstrap.runner import BootstrapError  # noqa: E402
from repo2rlenv.spec.input import BootstrapSpec, LLMSpec, RepoSpec  # noqa: E402

logger = logging.getLogger("bootstrap_matrix")

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"  # bootstrap ok, verify failed
STATUS_FAILED = "failed"

GLYPH = {
    STATUS_PENDING: "⏳",
    STATUS_RUNNING: "🔵",
    STATUS_SUCCESS: "🟢",
    STATUS_PARTIAL: "🟡",
    STATUS_FAILED: "🔴",
}


@dataclass
class Cell:
    repo: str  # owner/name
    tier: str
    language_note: str
    model_label: str
    provider: str
    model: str
    # outcome
    status: str = STATUS_PENDING
    iterations: int = 0
    cost_usd: float = 0.0
    verify_passed: bool | None = None
    verify_detail: str = ""
    image_tag: str = ""
    error: str = ""
    duration_sec: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def key(self) -> str:
        return f"{self.repo}::{self.model_label}"


@dataclass
class Manifest:
    concurrency: int
    budget: dict[str, Any]
    results_path: Path
    state_path: Path
    log_dir: Path
    models: dict[str, dict[str, Any]]  # label → {provider, model}
    tiers: list[dict[str, Any]]


def load_manifest(path: Path) -> Manifest:
    data = yaml.safe_load(path.read_text())
    models_raw = data["models"]
    # Normalize: accept either {label: {provider, model}} dict OR legacy list form.
    if isinstance(models_raw, list):
        models = {m["label"]: {"provider": m["provider"], "model": m["model"]} for m in models_raw}
    else:
        models = dict(models_raw)
    return Manifest(
        concurrency=int(data.get("concurrency", 4)),
        budget=dict(data.get("budget", {})),
        results_path=ROOT / data["results_path"],
        state_path=ROOT / data["state_path"],
        log_dir=ROOT / data["log_dir"],
        models=models,
        tiers=list(data["tiers"]),
    )


def build_cells(manifest: Manifest) -> list[Cell]:
    """One cell per repo, model assigned by the tier's `model` field.

    For cross-LLM regression on baselines, list the same repo under multiple
    tiers (each with a different `model`) — the cell key includes the model
    label so duplicates don't collide.
    """
    cells: list[Cell] = []
    for tier in manifest.tiers:
        model_label = tier.get("model")
        if not model_label:
            raise ValueError(f"tier {tier.get('name')!r} missing `model` field")
        if model_label not in manifest.models:
            raise ValueError(
                f"tier {tier.get('name')!r} references unknown model {model_label!r}; "
                f"defined models: {list(manifest.models)}"
            )
        m = manifest.models[model_label]
        for repo in tier["repos"]:
            cells.append(
                Cell(
                    repo=repo,
                    tier=tier["name"],
                    language_note=tier.get("language_note", ""),
                    model_label=model_label,
                    provider=m["provider"],
                    model=m["model"],
                )
            )
    return cells


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def hydrate_cells(cells: list[Cell], state: dict[str, dict[str, Any]]) -> None:
    for c in cells:
        s = state.get(c.key)
        if not s:
            continue
        for k, v in s.items():
            if hasattr(c, k):
                setattr(c, k, v)


def render_results_md(manifest: Manifest, cells: list[Cell]) -> str:
    by_tier: dict[str, list[Cell]] = {}
    for c in cells:
        by_tier.setdefault(c.tier, []).append(c)

    parts: list[str] = []
    parts.append("# v0.8.1 bootstrap robustness — results tracking sheet\n\n")
    parts.append("Updated by `scripts/bootstrap_matrix.py`.\n\n")

    parts.append("## Legend\n\n")
    parts.append("| Symbol | Meaning |\n|:-:|---|\n")
    parts.append(f"| {GLYPH[STATUS_PENDING]} | pending — not yet attempted |\n")
    parts.append(f"| {GLYPH[STATUS_RUNNING]} | running |\n")
    parts.append(f"| {GLYPH[STATUS_SUCCESS]} | success — bootstrap + verify both pass |\n")
    parts.append(f"| {GLYPH[STATUS_PARTIAL]} | partial — bootstrap succeeded but verify failed |\n")
    parts.append(
        f"| {GLYPH[STATUS_FAILED]} | failed — bootstrap agent gave up / hit budget / errored |\n\n"
    )

    parts.append("## Summary\n\n")
    n_total = len(cells)
    n_success = sum(1 for c in cells if c.status == STATUS_SUCCESS)
    n_partial = sum(1 for c in cells if c.status == STATUS_PARTIAL)
    n_failed = sum(1 for c in cells if c.status == STATUS_FAILED)
    n_pending = n_total - n_success - n_partial - n_failed
    parts.append(
        f"- Total cells: **{n_total}** "
        f"({n_success} 🟢, {n_partial} 🟡, {n_failed} 🔴, {n_pending} ⏳)\n"
    )
    total_cost = sum(c.cost_usd for c in cells)
    parts.append(f"- LLM spend so far: **${total_cost:.2f}**\n")
    parts.append("- Models: " + ", ".join(f"`{label}`" for label in manifest.models) + "\n\n")

    for tier_name in [t["name"] for t in manifest.tiers]:
        tier_cells = by_tier.get(tier_name, [])
        lang = tier_cells[0].language_note if tier_cells else ""
        parts.append(f"## {tier_name} ({lang})\n\n")
        parts.append("| Repo | Model | Status | Iters | Cost | Verify | Notes |\n")
        parts.append("|---|---|:-:|:-:|---:|:-:|---|\n")
        for c in tier_cells:
            verify = "—"
            if c.verify_passed is True:
                verify = "✅"
            elif c.verify_passed is False:
                verify = "❌"
            note = c.error or c.verify_detail
            note = note.replace("|", "\\|").replace("\n", " ")[:120] or "—"
            cost_cell = f"${c.cost_usd:.3f}" if c.cost_usd else "—"
            iters_cell = str(c.iterations) if c.iterations else "—"
            parts.append(
                f"| `{c.repo}` | {c.model_label} | {GLYPH[c.status]} | "
                f"{iters_cell} | {cost_cell} | {verify} | {note} |\n"
            )
        parts.append("\n")
    return "".join(parts)


def write_results_md(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def cell_to_state_entry(c: Cell) -> dict[str, Any]:
    return {
        "status": c.status,
        "iterations": c.iterations,
        "cost_usd": c.cost_usd,
        "verify_passed": c.verify_passed,
        "verify_detail": c.verify_detail,
        "image_tag": c.image_tag,
        "error": c.error,
        "duration_sec": c.duration_sec,
        "started_at": c.started_at,
        "finished_at": c.finished_at,
        "tier": c.tier,
        "language_note": c.language_note,
        "provider": c.provider,
        "model": c.model,
    }


_BOOTSTRAP_ERROR_RE = re.compile(r"iterations=(\d+).*?cost.\\?\$?([0-9.]+)")


def _run_cell_blocking(cell: Cell, manifest: Manifest, *, force: bool = False) -> Cell:
    """Run a single cell's bootstrap. Blocking; call via asyncio.to_thread.

    Per-cell logs are not captured here — the bootstrap module persists each
    agent's full transcript at `envs-matrix/<model>/<owner>__<name>/.../transcript.jsonl`
    which is the source of truth. The root logger handler we used previously
    captured interleaved output from all concurrent cells, which made the
    files actively misleading.

    `force=True` bypasses the bootstrap cache so the agent re-runs even when
    a prior partial/failed result is on disk.
    """
    repo_spec = RepoSpec(url=cell.repo)
    llm_spec = LLMSpec(provider=cell.provider, model=cell.model)
    boot_spec = BootstrapSpec(
        enabled=True,
        max_iterations=int(manifest.budget.get("max_iterations", 25)),
        max_seconds=int(manifest.budget.get("max_seconds", 1800)),
        max_llm_spend_usd=float(manifest.budget.get("max_llm_spend_usd", 2.0)),
        # Each cell gets its own cache slot so concurrent runs don't trample.
        cache_dir=ROOT / "envs-matrix" / cell.model_label,
    )

    # Reset prior-run fields so a successful retry doesn't carry stale error text
    cell.error = ""
    cell.verify_detail = ""
    cell.verify_passed = None
    cell.iterations = 0
    cell.cost_usd = 0.0
    cell.image_tag = ""

    cell.started_at = time.time()
    cell.status = STATUS_RUNNING
    t0 = time.monotonic()

    try:
        result = ensure_bootstrap(repo_spec, boot_spec, llm_spec, force=force)
        cell.iterations = result.iterations
        cell.cost_usd = result.llm_cost_estimate_usd
        cell.verify_passed = result.verify_passed
        cell.verify_detail = result.verify_detail
        cell.image_tag = result.image_tag
        cell.status = STATUS_SUCCESS if result.verify_passed else STATUS_PARTIAL
    except BootstrapError as exc:
        msg = str(exc)
        cell.error = msg[:300]
        cell.status = STATUS_FAILED
        # Parse iterations + cost from the runner's error message so the
        # tracking sheet shows real numbers instead of zeros for failed cells.
        m = _BOOTSTRAP_ERROR_RE.search(msg)
        if m:
            try:
                cell.iterations = int(m.group(1))
                cell.cost_usd = float(m.group(2))
            except (ValueError, IndexError):
                pass
    except Exception as exc:
        cell.error = f"{type(exc).__name__}: {str(exc)[:280]}\n{traceback.format_exc()[:500]}"
        cell.status = STATUS_FAILED
    finally:
        cell.duration_sec = round(time.monotonic() - t0, 2)
        cell.finished_at = time.time()
    return cell


async def run_matrix(
    manifest: Manifest,
    cells: list[Cell],
    *,
    only_pending: bool,
    rerun_keys: set[str],
    rerun_failed: bool,
    tier_filter: str | None,
    dry_run: bool,
) -> None:
    state = load_state(manifest.state_path)

    # Selection semantics:
    #   --rerun X       → run ONLY X (and any other --rerun cells). Ignore pending.
    #   --rerun-failed  → run ONLY 🔴 / 🟡 cells. Ignore pending.
    #   default         → run all PENDING cells.
    # Combination of --rerun + --rerun-failed → union of both sets.
    explicit_mode = bool(rerun_keys) or rerun_failed
    pending: list[Cell] = []
    for c in cells:
        if tier_filter and tier_filter not in c.tier:
            continue
        already = state.get(c.key, {})
        prior_status = already.get("status", STATUS_PENDING)
        if explicit_mode:
            if c.key in rerun_keys:
                pending.append(c)
                continue
            if rerun_failed and prior_status in (STATUS_FAILED, STATUS_PARTIAL):
                pending.append(c)
                continue
            continue  # explicit mode: skip everything else
        if prior_status == STATUS_PENDING:
            pending.append(c)

    print(f"plan: {len(pending)}/{len(cells)} cells to run ({manifest.concurrency} concurrent)")
    by_model: dict[str, int] = {}
    for c in pending:
        by_model[c.model_label] = by_model.get(c.model_label, 0) + 1
    print("  by model: " + ", ".join(f"{k}={v}" for k, v in by_model.items()))
    for c in pending:
        print(f"  - {c.repo} @ {c.model_label}  [{c.tier}]")
    if dry_run:
        return
    if not pending:
        print("nothing to do.")
        # still re-render results.md so any stale info gets refreshed
        write_results_md(manifest.results_path, render_results_md(manifest, cells))
        return

    sem = asyncio.Semaphore(manifest.concurrency)
    write_lock = asyncio.Lock()
    # If the user passed --rerun-failed or named cells, force-rerun those
    # so the bootstrap cache doesn't short-circuit the agent.
    force_keys = set(rerun_keys)
    if rerun_failed:
        for c in pending:
            if state.get(c.key, {}).get("status") in (STATUS_FAILED, STATUS_PARTIAL):
                force_keys.add(c.key)

    async def run_one(c: Cell) -> None:
        async with sem:
            force = c.key in force_keys
            tag = " [forced]" if force else ""
            print(f"[start] {c.repo} @ {c.model_label}{tag}")
            updated = await asyncio.to_thread(_run_cell_blocking, c, manifest, force=force)
            async with write_lock:
                state[updated.key] = cell_to_state_entry(updated)
                save_state(manifest.state_path, state)
                write_results_md(manifest.results_path, render_results_md(manifest, cells))
            mark = GLYPH[updated.status]
            print(
                f"[done ] {c.repo} @ {c.model_label} {mark} "
                f"iters={updated.iterations} cost=${updated.cost_usd:.3f} "
                f"verify={updated.verify_passed} ({updated.duration_sec:.0f}s)"
            )

    await asyncio.gather(*(run_one(c) for c in pending))

    print()
    print("matrix run complete.")
    print(f"  results: {manifest.results_path}")
    print(f"  state:   {manifest.state_path}")
    print(f"  logs:    {manifest.log_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description="v0.8.1 bootstrap matrix orchestrator")
    parser.add_argument("--manifest", default=str(HERE / "bootstrap_matrix.yaml"))
    parser.add_argument("--tier", default=None, help="substring match on tier name")
    parser.add_argument("--rerun", action="append", default=[], help="repo::model_label to rerun")
    parser.add_argument("--rerun-failed", action="store_true", help="rerun 🔴 and 🟡 cells")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    manifest = load_manifest(Path(args.manifest))
    manifest.log_dir.mkdir(parents=True, exist_ok=True)
    cells = build_cells(manifest)
    state = load_state(manifest.state_path)
    hydrate_cells(cells, state)

    asyncio.run(
        run_matrix(
            manifest,
            cells,
            only_pending=True,
            rerun_keys=set(args.rerun),
            rerun_failed=args.rerun_failed,
            tier_filter=args.tier,
            dry_run=args.dry_run,
        )
    )
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
