#!/usr/bin/env python
"""scripts/v083/build_launch.py — stitch the aggregate launch dataset.

Used by the v0.8.3 release-cut PR (NOT by any per-arc PR). After every arc
has pushed its per-pipeline dataset to HF Hub, this script:

  1. Walks each per-pipeline dataset under <sweep-dir>/<pipeline>/<repo-slug>/.
  2. Filters to tasks whose oracle reward (from <sweep>/.validation/) is 1.000.
  3. Optionally caps per-pipeline contribution (default 120 / pipeline) so
     no single pipeline dominates the headline dataset.
  4. Copies the chosen task directories into <out>/launch-dataset/.
  5. Optionally invokes `repo2rlenv push <out>/launch-dataset/ <repo-id>
     --require-registry` to land it on HF Hub with the Visualiser badge.

The final aggregate dataset is the v0.8.3 launch artifact:
  https://huggingface.co/datasets/<your-org>/repo2rlenv-v083-launch
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class TaskRef:
    """One verified task pointer: source path + provenance + reward."""

    pipeline: str
    repo: str
    src: Path
    reward: float


def _walk_task_dirs(d: Path) -> list[Path]:
    if not d.is_dir():
        return []
    return [p for p in sorted(d.iterdir()) if p.is_dir() and (p / "task.toml").exists()]


def _matching_jobs_dir(sweep_root: Path, pipeline: str, repo_slug: str) -> Path | None:
    cand = sweep_root / ".validation" / pipeline / repo_slug
    return cand if cand.exists() else None


def _reward_for_task(task_name: str, jobs_dir: Path) -> float | None:
    """Heuristic match — harbor names jobs after the task. Look for any reward.txt
    under a directory whose name contains `task_name`."""
    for reward_file in jobs_dir.rglob("verifier/reward.txt"):
        # Walk up to find the task-level dir (usually 2-3 levels up)
        parent_names = {p.name for p in reward_file.parents}
        if task_name in parent_names:
            try:
                return float(reward_file.read_text().strip())
            except (OSError, ValueError):
                return None
    return None


def collect_verified_tasks(*, sweep_root: Path, min_reward: float = 1.0) -> list[TaskRef]:
    """Walk <sweep>/<pipeline>/<repo-slug>/ and return every T3-passing task ref."""

    verified: list[TaskRef] = []
    if not sweep_root.exists():
        return verified

    for pipeline_dir in sorted(sweep_root.iterdir()):
        if not pipeline_dir.is_dir() or pipeline_dir.name.startswith("."):
            continue
        pipeline = pipeline_dir.name

        for repo_dir in sorted(pipeline_dir.iterdir()):
            if not repo_dir.is_dir():
                continue
            repo_slug = repo_dir.name
            jobs_dir = _matching_jobs_dir(sweep_root, pipeline, repo_slug)

            for task in _walk_task_dirs(repo_dir):
                # text-only pipelines (pr_diff) have no oracle — accept all T1-passing
                if pipeline == "pr_diff":
                    verified.append(
                        TaskRef(pipeline=pipeline, repo=repo_slug, src=task, reward=1.0)
                    )
                    continue
                if jobs_dir is None:
                    continue
                r = _reward_for_task(task.name, jobs_dir)
                if r is not None and r >= min_reward:
                    verified.append(TaskRef(pipeline=pipeline, repo=repo_slug, src=task, reward=r))
    return verified


def cap_per_pipeline(refs: list[TaskRef], cap: int) -> list[TaskRef]:
    """Keep at most `cap` refs per pipeline, in the order they appear."""
    counts: dict[str, int] = defaultdict(int)
    out: list[TaskRef] = []
    for r in refs:
        if counts[r.pipeline] >= cap:
            continue
        out.append(r)
        counts[r.pipeline] += 1
    return out


def stitch(refs: list[TaskRef], dest: Path) -> int:
    """Copy each ref into <dest>/<pipeline>__<repo_slug>__<task_name>/."""
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in refs:
        target = dest / f"{r.pipeline}__{r.repo}__{r.src.name}"
        if target.exists():
            continue
        shutil.copytree(r.src, target)
        n += 1
    return n


def write_manifest(refs: list[TaskRef], dest: Path) -> None:
    """Write a JSON manifest of contributions so we know what went in."""
    by_pipeline: dict[str, int] = defaultdict(int)
    for r in refs:
        by_pipeline[r.pipeline] += 1
    manifest = {
        "total_tasks": len(refs),
        "by_pipeline": dict(by_pipeline),
        "tasks": [
            {
                "pipeline": r.pipeline,
                "repo": r.repo,
                "task_name": r.src.name,
                "reward": r.reward,
            }
            for r in refs
        ],
    }
    (dest / "launch_manifest.json").write_text(json.dumps(manifest, indent=2))


def push_to_hub(local_dir: Path, repo_id: str, *, require_registry: bool) -> int:
    cmd = ["repo2rlenv", "push", str(local_dir), repo_id]
    if require_registry:
        cmd.append("--require-registry")
    print(f"[build_launch] $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="stitch the v0.8.3 launch dataset")
    p.add_argument("--sweep-dir", type=Path, required=True)
    p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="output dir; the launch dataset lives at <out>/launch-dataset/",
    )
    p.add_argument(
        "--cap-per-pipeline",
        type=int,
        default=120,
        help="max tasks contributed by any single pipeline (default: 120)",
    )
    p.add_argument(
        "--min-reward", type=float, default=1.0, help="oracle reward threshold (default 1.0)"
    )
    p.add_argument(
        "--push-to",
        help="HF Hub repo_id, e.g. <your-org>/repo2rlenv-v083-launch. Omit to stitch only.",
    )
    p.add_argument(
        "--require-registry",
        action="store_true",
        help="forwarded to `repo2rlenv push` when --push-to is set",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    refs = collect_verified_tasks(sweep_root=args.sweep_dir, min_reward=args.min_reward)
    if not refs:
        print(f"no verified tasks found under {args.sweep_dir}", file=sys.stderr)
        return 1
    print(f"[build_launch] collected {len(refs)} verified tasks")

    if args.cap_per_pipeline > 0:
        refs = cap_per_pipeline(refs, args.cap_per_pipeline)
        print(f"[build_launch] capped to {len(refs)} after cap={args.cap_per_pipeline}/pipeline")

    out_root = args.out / "launch-dataset"
    n_copied = stitch(refs, out_root)
    write_manifest(refs, out_root)
    print(f"[build_launch] stitched {n_copied} tasks into {out_root}")

    if args.push_to:
        rc = push_to_hub(out_root, args.push_to, require_registry=args.require_registry)
        if rc != 0:
            print(f"[build_launch] push failed (exit {rc})", file=sys.stderr)
            return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "TaskRef",
    "cap_per_pipeline",
    "collect_verified_tasks",
    "main",
    "stitch",
    "write_manifest",
]
