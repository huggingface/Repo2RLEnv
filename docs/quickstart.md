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
  --llm anthropic/claude-sonnet-4-6 \
  --out ./datasets/<dataset-name>
```

This will:
1. Clone the repo (`gh` for auth; private repos work the same way)
2. List merged PRs via `gh pr list`
3. For each PR, fetch its unified diff
4. Emit one Harbor task per PR — `task.toml` + `instruction.md` + `solution/patch.diff`

Output lands in `./datasets/<dataset-name>/<owner>__<repo>-<pr_number>/`.

## Push to HF Hub

Replace the `--out` flag with an `hf://` destination and the dataset is pushed after generation:

```bash
# 1. Generate to a local directory
repo2rlenv generate \
  --repo <owner>/<repo> \
  --pipeline pr_diff \
  --pipeline-opt limit=10 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./datasets/<dataset-name>

# 2. Push to HF Hub
repo2rlenv push ./datasets/<dataset-name> hf://<your-org>/<dataset-name>

# 3. Pull it back anywhere later
repo2rlenv pull hf://<your-org>/<dataset-name>
```

The push emits a `registry.json` so `harbor download --registry-url hf://<your-org>/<dataset-name>` works out of the box.

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

harbor run -d ./datasets/<dataset-name> -e local-docker -a oracle
# Or remote: -e modal / -e daytona / -e e2b / -e runloop
```

## Next steps

- **Different pipeline?** See [`pipelines/README.md`](./pipelines/README.md) for the menu.
- **Private repos?** Already work — `gh auth login` is the only setup. See [`reference/AUTH.md`](./reference/AUTH.md) for the resolution chain.
- **Sandbox-required pipelines** (`pr_runtime`, `commit_runtime`, ...): the runtime bootstraps a Docker image on demand. See [`reference/BOOTSTRAP.md`](./reference/BOOTSTRAP.md).
- **Build your own pipeline?** [`contributing/ADDING_A_PIPELINE.md`](./contributing/ADDING_A_PIPELINE.md).
