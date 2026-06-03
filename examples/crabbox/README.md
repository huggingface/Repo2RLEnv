# crabbox runner for pr_diff tasks

Score Repo2RLEnv `pr_diff` tasks on any [crabbox](https://github.com/openclaw/crabbox)
sandbox provider — local Docker, E2B, Modal, Daytona, islo.dev, Namespace
Devbox, Tensorlake — selected by a single `--provider` flag.

```sh
python3 examples/crabbox/runner.py <task_dir>                          # default: islo
python3 examples/crabbox/runner.py <task_dir> --provider local-container
python3 examples/crabbox/runner.py --all <dataset_dir> -j 8            # batch + CSV
```

The runner extracts the embedded verifier out of `environment/Dockerfile`,
stages it with the agent diff, runs `tests/test.sh` on the sandbox via
crabbox, and parses `reward.json` out of stdout. Output: `reward.json`
next to the task (single) or `rewards.csv` next to the dataset (batch).

## Supported providers

| `--provider`       | crabbox image flag we set | unit-tested | live-tested |
|--------------------|---------------------------|:-:|:-:|
| `local-container`  | `--local-container-image` | ✅ |   |
| `docker`           | alias for `local-container` | ✅ |   |
| `islo`             | `--islo-image`            | ✅ | ✅ |
| `e2b`              | `--e2b-template`          | ✅ |   |
| `modal`            | `--modal-image`           | ✅ |   |
| `daytona`          | `--daytona-snapshot`      | ✅ |   |
| `namespace-devbox` | `--namespace-image`       | ✅ |   |
| `tensorlake`       | `--tensorlake-image`      | ✅ |   |

Unsupported providers (`aws`, `azure`, `gcp`, `hetzner`, `proxmox`, `ssh`, …)
need a pre-baked VM image with `python` + `git`; the wrapper raises a
helpful `ValueError` listing the supported names rather than constructing
a doomed `crabbox` invocation.

## Prereqs

```sh
uv pip install repo2rlenv                           # this package
# crabbox CLI: https://github.com/openclaw/crabbox#install
# Plus auth for your chosen provider, e.g. `export ISLO_API_KEY=ak_...`
```

## Quickstart

```sh
# 1. Pull a published pr_diff dataset (or generate your own).
repo2rlenv pull AdithyaSK/repo2rlenv-pr-diff ./datasets/pr-diff

# 2. Oracle sanity check on the default provider (islo). Expect ~1.0.
python3 examples/crabbox/runner.py ./datasets/pr-diff/pallets__click-3466

# 3. Same task, local Docker — no cloud, no API key.
python3 examples/crabbox/runner.py ./datasets/pr-diff/pallets__click-3466 \
  --provider local-container

# 4. Score a real agent's diff, capture reward locally.
python3 examples/crabbox/runner.py ./datasets/pr-diff/pallets__click-3466 \
  --agent-patch ./agent_predicted.diff \
  --reward-out  ./agent_run.json

# 5. Forward env vars (e.g. ANTHROPIC_API_KEY) so the verifier's LLM-judge
#    component fires. --allow-env is repeatable.
python3 examples/crabbox/runner.py <task_dir> --allow-env ANTHROPIC_API_KEY

# 6. Whole dataset, parallel, CSV summary.
python3 examples/crabbox/runner.py --all ./datasets/pr-diff -j 8
```

## How it works

`pr_diff` tasks ship `environment/Dockerfile` with three verifier files
(`oracle.patch`, `verifier.py`, `instruction.md`) inlined as base64-echo
layers. Most providers (islo, e2b, …) are not docker-in-docker, so the
runner:

1. Parses `task.toml` (stdlib `tomllib`) for `repo`, `ref`, `pipeline`.
2. Extracts the three `/verifier/` files locally from the Dockerfile.
3. Stages them with the agent diff and `git init`s the dir (crabbox sync
   expects a git checkout).
4. Builds the right `crabbox run --provider <p> --<image-flag> python:3.12-slim
   --<workdir-flag> task` for the requested provider (per-provider flag map
   in `PROVIDER_CONFIG`).
5. The remote bash script clones the repo at `<ref>`, applies the agent
   diff (skipped if empty), runs `tests/test.sh` (with `/workspace`
   rewritten to `/repo`), then emits a sentinel and `cat`s
   `/logs/verifier/reward.json` over stdout.
6. The host parses the reward.json off the subprocess pipe and writes it
   to `reward.json`. Portable across every supported provider — islo's
   delegate-exec mode means `crabbox --download` / `--capture-stdout`
   don't work there, so stdout exfil is the lowest-common-denominator.

Batch mode (`--all`) does the above in a `ThreadPoolExecutor`; each task
gets its own sandbox lease.

## Tests

`tests/test_examples_crabbox.py` — 16 tests:

- **15 unit tests** (run in CI, no network or binary required): parametrize
  over every supported provider, monkeypatch `subprocess.run`, assert the
  exact `crabbox` argv. Cover task.toml parsing, verifier extraction,
  unknown-provider error, `--keep`, `--allow-env`, no-sentinel error path.
- **1 live islo smoke** (gated on `ISLO_API_KEY` + `crabbox` on PATH):
  pulls `pallets__click-3466`, scores the oracle, asserts
  `final_reward == 1.0`. Trigger locally with:

  ```sh
  ISLO_API_KEY=ak_... uv run pytest tests/test_examples_crabbox.py \
    -k live_islo -v
  ```

## Verified runs · live islo.dev sandbox, May 2026

| task | mode | wall | `final_reward` |
|---|---|---|---|
| `pallets__click-3466` | single, oracle | ~51 s | **1.0** |
| `pallets__click-3466` | single, empty diff | ~50 s | **0.0** |
| 3 tasks (click ×2 + chalk) | batch `-j 3` | ~57 s | **1.0** each |

## Scope

- **Supported:** `pr_diff` — every task in
  [`AdithyaSK/repo2rlenv-pr-diff`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff)
  (161 tasks).
- **Not yet:** `pr_runtime`, `commit_runtime`, `cve_patches`, and the other
  sandbox pipelines — they build a per-repo Docker image during
  `repo2rlenv bootstrap` and expect docker-in-docker. Wiring those through
  crabbox is a follow-up.
- **Not a Harbor `--env` backend.** That belongs upstream in
  [`harbor-framework/harbor`](https://github.com/harbor-framework/harbor),
  not here.
