# Quickstart

Turn a GitHub repo into a Harbor-shaped dataset, in about ten minutes.

## Prerequisites

```bash
# uv (for installs)
curl -LsSf https://astral.sh/uv/install.sh | sh

# gh CLI — handles GitHub auth for both public and private clones
brew install gh        # or: see https://cli.github.com
gh auth login

# An LLM key (any of the providers LiteLLM supports)
export ANTHROPIC_API_KEY=...   # or OPENAI_API_KEY / HF_TOKEN / ...

# (Optional) HF Hub login if you plan to push the dataset
huggingface-cli login
```

## Install

```bash
pip install repo2rlenv         # from PyPI
# or:
uv tool install repo2rlenv
```

## Generate a dataset

The shipped pipeline is `pr_diff` — SWE-RL-style PR mining, no Docker required.

```bash
repo2rlenv generate \
  --repo <owner>/<repo> \
  --pipeline pr_diff \
  --pipeline-opt limit=10 \
  --out ./datasets/<dataset-name>
```

This will:
1. Clone the repo (`gh` for auth; private repos work the same way)
2. List merged PRs via `gh pr list`
3. For each PR, fetch its unified diff
4. Emit one Harbor task per PR — `task.toml` + `instruction.md` + `solution/patch.diff`

Output lands in `./datasets/<dataset-name>/<owner>__<repo>-<pr_number>/`.

## Push to HF Hub

```bash
# 1. Generate to a local directory
repo2rlenv generate \
  --repo <owner>/<repo> \
  --pipeline pr_diff \
  --pipeline-opt limit=10 \
  --out ./datasets/<dataset-name>

# 2. Push to HF Hub (bare name auto-resolves owner via `whoami`)
repo2rlenv push ./datasets/<dataset-name> <your-org>/<dataset-name>

# 3. Pull it back anywhere later
repo2rlenv pull <your-org>/<dataset-name>
```

For sandbox-verified pipelines (`pr_runtime`, `mutation_bugs`, …), `repo2rlenv push` also uploads the bootstrap Docker image to a container registry (GHCR by default, auto-detected from `~/.docker/config.json`) and rewrites each task's Dockerfile to point at the registry-qualified digest. This makes the dataset fully reproducible on any machine:

```bash
# Anyone can pull + run your published dataset on a fresh machine:
harbor download --registry-url https://huggingface.co/datasets/<your-org>/<dataset-name>
harbor run --agent oracle --path ./<dataset-name>/<task-id>
```

Run `repo2rlenv push --check-auth` to verify your registry credentials before pushing. See [`docs/reference/REGISTRY_AUTH.md`](./reference/REGISTRY_AUTH.md) for per-registry login instructions.

## Validate the dataset

```bash
# Fast structural check — every task.toml parses + has required fields
repo2rlenv validate ./datasets/<dataset-name>
```

For diff-similarity scoring inside a training loop, import the Python function
directly instead of shelling out:

```python
from repo2rlenv.reward import calculate_diff_similarity_reward
reward, meta = calculate_diff_similarity_reward(oracle_diff_text, prediction_text)
```

(Test-execution rewards — `Mean = 1.000` etc. — come from `harbor run`, not from this package.)

## Run the dataset with Harbor

Repo2RLEnv emits Harbor-shaped tasks; running them is Harbor's job:

```bash
uv tool install harbor

harbor run --path ./datasets/<dataset-name> --env docker --agent oracle
# Or remote: --env modal / --env daytona / --env e2b / --env runloop
```

> **Note:** `harbor run` requires a sandbox-verified pipeline (`pr_runtime`, `commit_runtime`, …).
> The `pr_diff` pipeline used above is text-only and does not emit an `environment/` directory,
> so Harbor cannot execute it. Switch to `--pipeline pr_runtime` (requires Docker + `--llm`) to
> produce runnable tasks.

## Next steps

- **Different pipeline?** See [`pipelines/README.md`](./pipelines/README.md) for the menu.
- **Private repos?** Already work — `gh auth login` is the only setup. See [`reference/AUTH.md`](./reference/AUTH.md) for the resolution chain.
- **Sandbox-required pipelines** (`pr_runtime`, `commit_runtime`, ...): the runtime bootstraps a Docker image on demand. See [`reference/BOOTSTRAP.md`](./reference/BOOTSTRAP.md).
- **Build your own pipeline?** [`contributing/ADDING_A_PIPELINE.md`](./contributing/ADDING_A_PIPELINE.md).
