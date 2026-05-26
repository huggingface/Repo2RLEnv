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

# Run with the oracle adapter — applies the gold patch, must score reward=1.0
harbor run -p ./datasets/<dataset-name> -a oracle --env docker

# Or with a real coding agent. We show `claude-code` here because that's
# what we used to verify our reference datasets, but Harbor ships 25+
# agent harnesses — swap `-a claude-code -m anthropic/claude-sonnet-4-6`
# for any of:
#   openhands / openhands-sdk · codex · aider · gemini-cli · copilot-cli
#   opencode · cursor-cli · qwen-coder · kimi-cli · goose · mini-swe-agent
#   swe-agent · nemo-agent · terminus-2 · trae-agent · devin · ... etc.
# Each agent expects its own provider env var (OPENAI_API_KEY,
# GOOGLE_API_KEY, GITHUB_TOKEN for copilot, …) — see `harbor run --help`
# for the full list. The verifier's LLM-judge component (when enabled)
# also needs ANTHROPIC_API_KEY — pass via --ve so it reaches the verifier
# container.
harbor run \
  -p ./datasets/<dataset-name> \
  -a claude-code -m anthropic/claude-sonnet-4-6 \
  --ak max_budget_usd=2.00 \
  --ae ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  --ve ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  --env docker

# Same env, different agent — openhands with GPT-4o:
harbor run \
  -p ./datasets/<dataset-name> \
  -a openhands -m openai/gpt-4o \
  --ae OPENAI_API_KEY=$OPENAI_API_KEY \
  --ve ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  --env docker

# Remote sandboxes: --env modal / --env daytona / --env e2b / --env runloop
```

`pr_diff` ships a thin `python:3.12-slim` environment with a multi-component
diff-similarity verifier baked in. For sandbox-verified pipelines like
`pr_runtime` / `commit_runtime` that need to actually execute the repo's test
suite, the runtime bootstraps a per-repo Docker image on demand — see
[`reference/BOOTSTRAP.md`](./reference/BOOTSTRAP.md).

## Next steps

- **Different pipeline?** See [`pipelines/README.md`](./pipelines/README.md) for the menu.
- **Private repos?** Already work — `gh auth login` is the only setup. See [`reference/AUTH.md`](./reference/AUTH.md) for the resolution chain.
- **Sandbox-required pipelines** (`pr_runtime`, `commit_runtime`, ...): the runtime bootstraps a Docker image on demand. See [`reference/BOOTSTRAP.md`](./reference/BOOTSTRAP.md).
- **Build your own pipeline?** [`contributing/ADDING_A_PIPELINE.md`](./contributing/ADDING_A_PIPELINE.md).
