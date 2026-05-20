# scripts/v083/ — sweep harness for the v0.8.3 launch

Internal launch tooling, not part of the published package.

The full plan lives at [`plans/v0.8.3_pipeline_optimization.md`](../../plans/v0.8.3_pipeline_optimization.md).
This directory is the **execution layer** for that plan: one driver, one
aggregator, one launch-dataset stitcher, one manifest.

## Files

| File | Purpose |
|---|---|
| `repos.yaml` | 38-repo launch composition (Tier A SWE-bench, Tier B HF ecosystem, Tier C multi-lang). Language + per-repo pipeline gates baked in. |
| `sweep.py` | Per-(pipeline, repo) state machine. Runs **generate → T1 → T2 → T3 → T4**, idempotent via `state.json`, concurrency cap. |
| `aggregate.py` | Walks sweep artifacts, emits `findings-<pipeline>.md` + `report-all.md`. Pure file IO — no LLM, no docker. |
| `build_launch.py` | Stitches the verified union of all per-pipeline datasets into the aggregate launch dataset. Used by the release-cut PR. |

## Prerequisites

```bash
# library + CLI
uv sync

# harbor for T2/T3/T4
uv tool install harbor

# .env (or shell exports) for ANTHROPIC_API_KEY + HF_TOKEN
```

## Run a single arc

```bash
# pr_diff (text-only — no docker, no T3/T4)
uv run python scripts/v083/sweep.py \
  --pipeline pr_diff \
  --out ./datasets/sweep-v083/ \
  --envs-per-cell 4 \
  --skip-t4

# pr_runtime (full 4-tier loop)
uv run python scripts/v083/sweep.py \
  --pipeline pr_runtime \
  --out ./datasets/sweep-v083/ \
  --envs-per-cell 3 \
  --concurrency 4

# Restrict to a subset of repos for a smoke run
uv run python scripts/v083/sweep.py \
  --pipeline pr_diff \
  --out ./datasets/sweep-v083/ \
  --envs-per-cell 1 \
  --repos pallets/click urfave/cli clap-rs/clap \
  --skip-t4
```

## Aggregate results

```bash
uv run python scripts/v083/aggregate.py \
  --sweep-dir ./datasets/sweep-v083/ \
  --out ./docs/release_notes/v0.8.3/
```

## Stitch the launch dataset (release-cut PR only)

```bash
uv run python scripts/v083/build_launch.py \
  --sweep-dir ./datasets/sweep-v083/ \
  --out ./datasets/v083-launch/ \
  --cap-per-pipeline 120 \
  --push-to <your-org>/repo2rlenv-v083-launch \
  --require-registry
```

## Layout of a sweep on disk

```
<--out>/
├── state.json                      # resume marker
├── <pipeline>/<repo-slug>/         # generated tasks (per cell)
│   └── <task-id>/
│       ├── task.toml
│       ├── instruction.md
│       ├── tests/   (most pipelines)
│       ├── environment/ (runtime pipelines)
│       └── .r2e_check.json (T2 result, when run)
├── .validation/<pipeline>/<repo-slug>/   # harbor oracle jobs-dir
└── .eval/<pipeline>/<repo-slug>/         # harbor claude-code jobs-dir
```

## Quirks

- **pr_diff is text-only.** No `tests/`, no `environment/`, so T2 (which
  needs `tests/`) and T3/T4 (which need `environment/`) don't apply. The
  driver auto-skips them.
- **Idempotency**: re-running the driver re-uses any cell whose `step ==
  "done"` from `state.json`. Delete `state.json` to force a full re-sweep.
- **Concurrency cap default = 4.** Docker builds contend hard above that.
- **Hard cost stop** at `--hard-stop-usd` (default 1500) — when the sweep's
  total LLM cost crosses that threshold, in-flight cells finish and no new
  cells are scheduled.

## Why this lives under `scripts/`

It's launch infrastructure, not a public API. We don't ship it on PyPI.
Tests for these scripts live at `tests/scripts/test_v083_harness.py` and
ride the normal `pytest` invocation.
