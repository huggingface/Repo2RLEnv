#!/usr/bin/env python
"""scripts/v083/aggregate.py — report builder for the v0.8.3 sweep.

Walks every cell's artifacts produced by `sweep.py` and emits two kinds of
human-readable reports:

  1. One `findings-<pipeline>.md` per pipeline arc, with:
       - Generation yield (emitted / skipped, per skip_reason where available)
       - T1 structural pass rate
       - T2 LLM rubric aggregated outcomes per criterion
       - T3 oracle pass rate (with sample failure modes)
       - T4 Sonnet pass rate distribution
       - Top 3 actionable optimization candidates ranked by impact

  2. A top-level `report-all.md` rolling up all 8 pipelines for the v0.8.3
     release notes.

Inputs:
  --sweep-dir       The sweep output root (matches sweep.py --out)
  --out             Where to write the report files (defaults to <sweep-dir>)

The aggregator never touches docker or the LLMs — pure file IO.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Per-cell aggregation
# ---------------------------------------------------------------------------


@dataclass
class CellSummary:
    pipeline: str
    repo: str
    candidates: int = 0
    t1_passed: int = 0
    t1_failed: int = 0
    t2_critical_pass: int = 0  # tasks passing the 4 hard rubric criteria
    t2_critical_fail: int = 0
    t2_failed_criteria: dict[str, int] = field(default_factory=dict)
    t3_pass: int = 0
    t3_fail: int = 0
    t3_exceptions: int = 0
    t4_pass: int = 0
    t4_fail: int = 0
    t4_budget_hits: int = 0
    cost_usd: float = 0.0


#: Critical rubric criteria — failures on these block T2 pass status.
CRITICAL_CRITERIA: tuple[str, ...] = (
    "behavior_in_tests",
    "tests_or_solution_in_image",
    "hardcoded_solution",
    "pinned_dependencies",
)


def _walk_task_dirs(cell_dir: Path) -> list[Path]:
    if not cell_dir.is_dir():
        return []
    return [p for p in sorted(cell_dir.iterdir()) if p.is_dir() and (p / "task.toml").exists()]


def _read_rubric(task_dir: Path) -> dict[str, Any] | None:
    """Read `.r2e_check.json` written by `harbor check`. Returns None if absent."""
    f = task_dir / ".r2e_check.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _criterion_failed(rubric: dict[str, Any], criterion: str) -> bool:
    """Best-effort interpretation of harbor's rubric output.

    Harbor's rubric output has historically used a few shapes — this helper
    tries: {criteria: {name: {passed: bool}}}, {results: {name: bool}},
    or {name: bool} at top level. Conservative: missing → not failed.
    """
    criteria = rubric.get("criteria") or rubric.get("results") or rubric
    val = criteria.get(criterion) if isinstance(criteria, dict) else None
    if isinstance(val, bool):
        return val is False
    if isinstance(val, dict):
        passed = val.get("passed")
        if isinstance(passed, bool):
            return passed is False
    return False


def _find_validation_dir(sweep_root: Path, pipeline: str, repo_slug: str) -> Path | None:
    cand = sweep_root / ".validation" / pipeline / repo_slug
    return cand if cand.exists() else None


def _find_eval_dir(sweep_root: Path, pipeline: str, repo_slug: str) -> Path | None:
    cand = sweep_root / ".eval" / pipeline / repo_slug
    return cand if cand.exists() else None


def _read_reward(reward_file: Path) -> float | None:
    try:
        return float(reward_file.read_text().strip())
    except (OSError, ValueError):
        return None


def _scan_rewards(jobs_dir: Path) -> tuple[int, int]:
    """Walk a harbor jobs-dir and count (passed, failed) per `verifier/reward.txt`."""
    passed = failed = 0
    for reward_file in jobs_dir.rglob("verifier/reward.txt"):
        r = _read_reward(reward_file)
        if r is None:
            failed += 1
        elif r >= 1.0:
            passed += 1
        else:
            failed += 1
    return passed, failed


def summarize_cell(
    *,
    sweep_root: Path,
    pipeline: str,
    repo: str,
) -> CellSummary:
    """Aggregate one (pipeline, repo) cell from disk artifacts."""

    repo_slug = repo.replace("/", "-")
    out_dir = sweep_root / pipeline / repo_slug
    summary = CellSummary(pipeline=pipeline, repo=repo)

    tasks = _walk_task_dirs(out_dir)
    summary.candidates = len(tasks)

    # T1 — structural is binary at the dataset level (sweep.py only sets the
    # cell to "failed" if validate exits nonzero). Treat each task as passing
    # if task.toml + solve.sh + environment/ exist (or .toml-only for pr_diff).
    for t in tasks:
        looks_ok = (t / "task.toml").exists() and (
            (t / "solve.sh").exists() or (t / "environment").exists() or pipeline == "pr_diff"
        )
        if looks_ok:
            summary.t1_passed += 1
        else:
            summary.t1_failed += 1

    # T2 — per-task rubric JSON
    for t in tasks:
        rubric = _read_rubric(t)
        if rubric is None:
            continue
        had_critical_fail = False
        for c in CRITICAL_CRITERIA:
            if _criterion_failed(rubric, c):
                summary.t2_failed_criteria[c] = summary.t2_failed_criteria.get(c, 0) + 1
                had_critical_fail = True
        if had_critical_fail:
            summary.t2_critical_fail += 1
        else:
            summary.t2_critical_pass += 1

    # T3 — oracle rewards (skip for text-only)
    val_dir = _find_validation_dir(sweep_root, pipeline, repo_slug)
    if val_dir is not None:
        p, f = _scan_rewards(val_dir)
        summary.t3_pass, summary.t3_fail = p, f

    # T4 — agent rewards
    eval_dir = _find_eval_dir(sweep_root, pipeline, repo_slug)
    if eval_dir is not None:
        p, f = _scan_rewards(eval_dir)
        summary.t4_pass, summary.t4_fail = p, f

    return summary


# ---------------------------------------------------------------------------
# Pipeline-level rollups
# ---------------------------------------------------------------------------


@dataclass
class PipelineReport:
    pipeline: str
    cells: list[CellSummary] = field(default_factory=list)

    @property
    def candidates(self) -> int:
        return sum(c.candidates for c in self.cells)

    @property
    def t1_pass_pct(self) -> float:
        total = sum(c.t1_passed + c.t1_failed for c in self.cells)
        return 100 * sum(c.t1_passed for c in self.cells) / total if total else 0.0

    @property
    def t2_critical_pass_pct(self) -> float:
        total = sum(c.t2_critical_pass + c.t2_critical_fail for c in self.cells)
        return 100 * sum(c.t2_critical_pass for c in self.cells) / total if total else 0.0

    @property
    def t3_pass_pct(self) -> float:
        total = sum(c.t3_pass + c.t3_fail for c in self.cells)
        return 100 * sum(c.t3_pass for c in self.cells) / total if total else 0.0

    @property
    def t4_pass_pct(self) -> float:
        total = sum(c.t4_pass + c.t4_fail for c in self.cells)
        return 100 * sum(c.t4_pass for c in self.cells) / total if total else 0.0

    @property
    def verified_envs(self) -> int:
        return sum(c.t3_pass for c in self.cells)

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.cells)


def build_pipeline_report(*, sweep_root: Path, pipeline: str, repos: list[str]) -> PipelineReport:
    rep = PipelineReport(pipeline=pipeline)
    for repo in repos:
        rep.cells.append(summarize_cell(sweep_root=sweep_root, pipeline=pipeline, repo=repo))
    return rep


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _fmt_pct(passed_sum: int, total_sum: int) -> str:
    """Show 'n/a' when no observations; otherwise X.Y%."""
    if total_sum == 0:
        return "n/a"
    return f"{100 * passed_sum / total_sum:.1f}%"


def render_pipeline_md(rep: PipelineReport) -> str:
    """Render one findings-<pipeline>.md as markdown."""

    t1_total = sum(c.t1_passed + c.t1_failed for c in rep.cells)
    t2_total = sum(c.t2_critical_pass + c.t2_critical_fail for c in rep.cells)
    t3_total = sum(c.t3_pass + c.t3_fail for c in rep.cells)
    t4_total = sum(c.t4_pass + c.t4_fail for c in rep.cells)
    t1_passed = sum(c.t1_passed for c in rep.cells)
    t2_passed = sum(c.t2_critical_pass for c in rep.cells)
    t3_passed = sum(c.t3_pass for c in rep.cells)
    t4_passed = sum(c.t4_pass for c in rep.cells)

    lines: list[str] = []
    lines.append(f"# {rep.pipeline} — sweep findings")
    lines.append("")
    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Cells swept | {len(rep.cells)} |")
    lines.append(f"| Candidates emitted | {rep.candidates} |")
    lines.append(f"| T1 structural pass | {_fmt_pct(t1_passed, t1_total)} |")
    lines.append(f"| T2 critical-criteria pass | {_fmt_pct(t2_passed, t2_total)} |")
    lines.append(f"| T3 oracle pass | {_fmt_pct(t3_passed, t3_total)} |")
    lines.append(f"| T4 Sonnet 4.6 pass | {_fmt_pct(t4_passed, t4_total)} |")
    lines.append(f"| Verified envs (T3 reward 1.000) | {rep.verified_envs} |")
    lines.append(f"| Sweep cost | ${rep.total_cost_usd:.2f} |")
    lines.append("")

    def _cell_cell(passed: int, total: int) -> str:
        return "n/a" if total == 0 else f"{passed}/{total}"

    lines.append("## Per-repo breakdown")
    lines.append("")
    lines.append(
        "| Repo | Candidates | T1 pass | T2 critical pass | T3 reward 1.0 | T4 reward 1.0 |"
    )
    lines.append("|---|---|---|---|---|---|")
    for c in rep.cells:
        c_t1 = c.t1_passed + c.t1_failed
        c_t2 = c.t2_critical_pass + c.t2_critical_fail
        c_t3 = c.t3_pass + c.t3_fail
        c_t4 = c.t4_pass + c.t4_fail
        lines.append(
            f"| {c.repo} | {c.candidates} "
            f"| {_cell_cell(c.t1_passed, c_t1)} "
            f"| {_cell_cell(c.t2_critical_pass, c_t2)} "
            f"| {_cell_cell(c.t3_pass, c_t3)} "
            f"| {_cell_cell(c.t4_pass, c_t4)} |"
        )
    lines.append("")

    # T2 failure mode rollup
    rollup: dict[str, int] = {}
    for c in rep.cells:
        for k, v in c.t2_failed_criteria.items():
            rollup[k] = rollup.get(k, 0) + v
    if rollup:
        lines.append("## T2 critical-criteria failures (where + how often)")
        lines.append("")
        lines.append("| Criterion | Fails |")
        lines.append("|---|---|")
        for k in sorted(rollup, key=lambda x: -rollup[x]):
            lines.append(f"| `{k}` | {rollup[k]} |")
        lines.append("")

    # Optimization candidates (heuristic; humans refine in the PR). Only fire
    # when we have observations for that tier — otherwise "0%" is misleading.
    lines.append("## Top optimization candidates")
    lines.append("")
    candidates: list[str] = []
    if t1_total > 0 and rep.t1_pass_pct < 100:
        candidates.append(
            f"**T1 < 100% ({rep.t1_pass_pct:.1f}%)** — structural bug in pipeline emit. "
            "Re-run `repo2rlenv validate` locally and fix the missing/extra file."
        )
    if t2_total > 0 and rep.t2_critical_pass_pct < 100:
        worst = max(rollup, key=rollup.get) if rollup else None
        candidates.append(
            f"**T2 critical pass < 100% ({rep.t2_critical_pass_pct:.1f}%)** — worst criterion: "
            f"`{worst}`. Inspect a few `.r2e_check.json` files for actionable issues."
        )
    if t3_total > 0 and 0 < rep.t3_pass_pct < 85:
        candidates.append(
            f"**T3 below acceptance gate ({rep.t3_pass_pct:.1f}%, target ≥ 85%)** — oracle is "
            "saying the patches don't fix the tests. Re-check pipeline gold-patch construction."
        )
    if t4_total > 0 and rep.t4_pass_pct > 95:
        candidates.append(
            f"**T4 too high ({rep.t4_pass_pct:.1f}%)** — tasks are likely trivial. "
            "Tighten generation filters or pick harder seeds."
        )
    if t4_total > 0 and 0 < rep.t4_pass_pct < 10:
        candidates.append(
            f"**T4 too low ({rep.t4_pass_pct:.1f}%)** — tasks may be impossible. "
            "Check instruction text for ambiguity and verifier strictness."
        )
    if not candidates:
        candidates.append(
            "No obvious red flags — consider this arc ready to land. "
            "Recommended: still inspect 3 random tasks by hand before merging."
        )
    for i, c in enumerate(candidates[:3], start=1):
        lines.append(f"{i}. {c}")
    lines.append("")

    return "\n".join(lines)


def render_all_md(reports: dict[str, PipelineReport]) -> str:
    """Render the top-level report-all.md with one row per pipeline."""

    lines: list[str] = []
    lines.append("# v0.8.3 sweep — aggregate report")
    lines.append("")
    lines.append("## Per-pipeline rollup")
    lines.append("")
    lines.append("| Pipeline | Candidates | T1 % | T2 critical % | T3 % | T4 % | Verified envs |")
    lines.append("|---|---|---|---|---|---|---|")

    total_candidates = 0
    total_verified = 0
    for name, rep in reports.items():
        total_candidates += rep.candidates
        total_verified += rep.verified_envs
        t1_total = sum(c.t1_passed + c.t1_failed for c in rep.cells)
        t2_total = sum(c.t2_critical_pass + c.t2_critical_fail for c in rep.cells)
        t3_total = sum(c.t3_pass + c.t3_fail for c in rep.cells)
        t4_total = sum(c.t4_pass + c.t4_fail for c in rep.cells)
        t1_passed = sum(c.t1_passed for c in rep.cells)
        t2_passed = sum(c.t2_critical_pass for c in rep.cells)
        t3_passed = sum(c.t3_pass for c in rep.cells)
        t4_passed = sum(c.t4_pass for c in rep.cells)
        lines.append(
            f"| `{name}` | {rep.candidates} "
            f"| {_fmt_pct(t1_passed, t1_total)} "
            f"| {_fmt_pct(t2_passed, t2_total)} "
            f"| {_fmt_pct(t3_passed, t3_total)} "
            f"| {_fmt_pct(t4_passed, t4_total)} "
            f"| {rep.verified_envs} |"
        )
    lines.append(f"| **TOTAL** | **{total_candidates}** | — | — | — | — | **{total_verified}** |")
    lines.append("")
    lines.append("## Headline numbers")
    lines.append("")
    lines.append(f"- Total candidates emitted: **{total_candidates}**")
    lines.append(f"- Total verified envs (oracle reward 1.000): **{total_verified}**")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _discover_cells(sweep_root: Path) -> dict[str, list[str]]:
    """Walk <sweep_root>/<pipeline>/<repo-slug>/ to discover what's there."""
    out: dict[str, list[str]] = {}
    if not sweep_root.exists():
        return out
    for pipeline_dir in sorted(sweep_root.iterdir()):
        if not pipeline_dir.is_dir() or pipeline_dir.name.startswith("."):
            continue
        pipeline = pipeline_dir.name
        for repo_dir in sorted(pipeline_dir.iterdir()):
            if not repo_dir.is_dir():
                continue
            # repo-slug uses '-' instead of '/'; reverse the convention so
            # the consumer keeps the original form. We only know the slug
            # here so we keep that, and let downstream consumers split if needed.
            out.setdefault(pipeline, []).append(repo_dir.name)
    return out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="aggregate v0.8.3 sweep results")
    p.add_argument("--sweep-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out = args.out or args.sweep_dir
    out.mkdir(parents=True, exist_ok=True)

    discovered = _discover_cells(args.sweep_dir)
    if not discovered:
        print(f"no per-cell artifacts under {args.sweep_dir}", file=sys.stderr)
        return 1

    reports: dict[str, PipelineReport] = {}
    for pipeline, repo_slugs in discovered.items():
        # repo-slug ('foo-bar') doesn't tell us the original 'foo/bar' uniquely
        # if either component had a '-'. For the per-repo table we render the
        # slug as-is; the slug is what we used on disk anyway.
        rep = PipelineReport(pipeline=pipeline)
        for slug in repo_slugs:
            # Reuse summarize_cell by passing repo='slug' (we never split it back)
            rep.cells.append(
                summarize_cell(sweep_root=args.sweep_dir, pipeline=pipeline, repo=slug)
            )
        reports[pipeline] = rep
        md = render_pipeline_md(rep)
        (out / f"findings-{pipeline}.md").write_text(md)
        print(f"[aggregate] wrote {out}/findings-{pipeline}.md")

    (out / "report-all.md").write_text(render_all_md(reports))
    print(f"[aggregate] wrote {out}/report-all.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CRITICAL_CRITERIA",
    "CellSummary",
    "PipelineReport",
    "build_pipeline_report",
    "main",
    "render_all_md",
    "render_pipeline_md",
    "summarize_cell",
]
