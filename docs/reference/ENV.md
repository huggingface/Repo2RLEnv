---
title: "Environment variables"
---

Every variable Repo2RLEnv reads, in one place. None of them are required to *use* the tool — sensible defaults exist for all of them — but you'll want to know what's available when wiring up CI, Docker images, or a cron host.

Variables are grouped by what they affect.

## Storage paths

| Variable | What it controls | Default |
|---|---|---|
| `R2E_CACHE_DIR` | Bootstrap image cache root — where the LLM-built per-repo Docker images are stored, keyed by content hash. The expensive step runs once per (repo, ref); subsequent generations reuse the cache. | `./workspace/bootstrap` |

`repo2rlenv bootstrap --cache-dir DIR` and `repo2rlenv generate --bootstrap-opt cache_dir=DIR` take precedence over the env var, and the env var takes precedence over the default — standard layering.

> The dataset output path (`--out`) and any project-local state are intentionally **not** env-controlled: those are per-invocation choices that should live in your generate command or `Makefile`, not in shell state.

## GitHub auth

Used by every pipeline (mining + cloning).

| Variable | What it does |
|---|---|
| `GITHUB_TOKEN` | Personal access token. Read in the **third** position of the auth chain — after an explicitly-named token (`repo.auth_token_env` in your config) and after `gh auth token` if `gh` is installed and logged in. |
| `GH_TOKEN` | Not read directly. `gh` honors it, so when `gh` is installed it reaches Repo2RLEnv through `gh auth token` (second position). |
| `GITLAB_TOKEN` | Token for `gitlab.com` sources, read after `repo.auth_token_env`. |
| `repo.auth_token_env` *(config field, not env)* | Names which env var holds the token for *this* repo (useful when you have multiple org-scoped tokens). The token *value* is never embedded in config — only the *name*. |

Full resolution order + private-repo build-arg flow: [`AUTH.md`](./AUTH.md).

## Hugging Face Hub

For `repo2rlenv push`, `pull` and `release publish`. Repo2RLEnv reads the token file that `hf auth login` writes (`~/.cache/huggingface/token`) first, then `HF_TOKEN`.

| Variable | What it does |
|---|---|
| `HF_TOKEN` | Hub access token, used when `~/.cache/huggingface/token` doesn't exist. Push needs **write** scope on the target namespace; `pull` of a public dataset needs no token. |

## LLM providers

LiteLLM-resolved; per-provider defaults. Override with `--llm-key-env VAR` (or `llm.api_key_env` in config) if you use non-default names.

| Variable | Provider |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic (Claude) |
| `OPENAI_API_KEY` | OpenAI |
| `HF_TOKEN` | Hugging Face Router |
| `TOGETHER_API_KEY` | Together |
| `GROQ_API_KEY` | Groq |

Those five are resolved by `repo2rlenv` itself, so a missing key fails fast with the variable named. Every other LiteLLM provider resolves its own credentials inside LiteLLM (e.g. `OPENROUTER_API_KEY`, AWS credentials for Bedrock). Self-hosted servers need none:

| Variable | Provider |
|---|---|
| `HOSTED_VLLM_API_KEY` *(optional)* | `hosted_vllm/…` — only if your vLLM was started with `--api-key`; honoured with or without `--llm-endpoint`. `HOSTED_VLLM_API_BASE` is the alternative to `--llm-endpoint`. |
| `OLLAMA_API_KEY` *(optional)* | `ollama/…` — `OLLAMA_API_BASE` defaults to `http://localhost:11434`. |

With `--llm-endpoint` (or `llm.endpoint` in config) the provider-default key — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, … — is never forwarded to the custom server: `openai/<model>` gets a placeholder, `hosted_vllm/` and `ollama/` use LiteLLM's own lookup above. Pass `--llm-key-env VAR` to send a specific key.

## Container registry (for `_runtime` image distribution on push)

Resolved before the docker credstore — an explicit env var beats whatever's cached locally, which is the right precedence for CI.

| Variable | What it does |
|---|---|
| `DOCKER_USERNAME` *(or `DOCKERHUB_USERNAME`)* | Docker Hub user. The push namespace is this user's namespace, **not** the HF dataset owner. |
| `DOCKER_TOKEN` *(or `DOCKERHUB_TOKEN`)* | Docker Hub PAT. Preferred over the docker credstore's OAuth identity token (the credstore token is often pull-only). |
| `GHCR_TOKEN` | GHCR token; falls back to `GITHUB_TOKEN`. One-time setup: `gh auth refresh -h github.com -s write:packages`. |
| `GITHUB_TOKEN` | GHCR fallback (above) **and** GitHub auth (above). |
| `GITHUB_ACTOR` | GHCR username when `GHCR_TOKEN` is set without a separate username; defaults to `x-access-token` if unset. |
| `DOCKER_CONFIG` | Path to a custom `docker/config.json` (standard Docker env var). |

Full L1-L4 probe protocol + per-registry setup: [`REGISTRY_AUTH.md`](./REGISTRY_AUTH.md).

## `pr_diff` reward tuning

The diff-similarity verifier baked into every `pr_diff` task is configurable at *score time* without rebuilding the image — the verifier reads these inside the container. Pass them to the verifier with Harbor's `--ve`, for example `harbor run … --ve R2E_W_JUDGE=0`.

| Variable | What it does | Default |
|---|---|---|
| `R2E_W_FORMAT` | Weight for the *format-valid* component (does the diff parse?). | `0.00` |
| `R2E_W_SIZE` | Weight for the *size sanity* component. | `0.08` |
| `R2E_W_FILE` | Weight for the *file-targeting* component (F1 over touched files). | `0.12` |
| `R2E_W_REGION` | Weight for the *region overlap* component. | `0.20` |
| `R2E_W_SIM` | Weight for the *changes-only similarity* component. | `0.10` |
| `R2E_W_JUDGE` | Weight for the *LLM-as-judge* semantic-correctness component. | `0.50` |
| `R2E_JUDGE_MODEL` | The judge model, as the serving API names it (a bare model id, not a LiteLLM `provider/model` string — the verifier is stdlib-only and does not go through LiteLLM). Required when `R2E_JUDGE_ENDPOINT` is set. | `claude-haiku-4-5-20251001` |
| `R2E_JUDGE_ENDPOINT` | Base URL of an OpenAI-compatible server to use as the judge instead of Anthropic — vLLM, Ollama, llama.cpp, a gateway. The verifier posts to `<endpoint>/chat/completions` at temperature 0 (small local models are noisy judges at their default sampling temperature). From inside the verifier container a model on the host is `http://host.docker.internal:8000/v1` on Docker Desktop (macOS / Windows / WSL2); on a bare Linux daemon that name does not resolve, so use the host's LAN IP (`hostname -I`) with the server bound to `0.0.0.0`. | unset (Anthropic) |
| `R2E_JUDGE_API_KEY` | Bearer token sent to `R2E_JUDGE_ENDPOINT`. Optional: self-hosted servers ignore it, so a placeholder is sent when unset. `ANTHROPIC_API_KEY` is never forwarded to a custom endpoint. | unset |
| `ANTHROPIC_API_KEY` | Required for the LLM-judge component on the default Anthropic route; the verifier degrades gracefully (records `judge_status=no_api_key`) when unset, so the other five components still score. Ignored when `R2E_JUDGE_ENDPOINT` is set. |

## Remote workers (Modal, Daytona)

Read by the provider SDKs when `repo2rlenv workers start`, a research recipe, Tasksmith or the quality loop creates or reconnects to a worker. Repo2RLEnv passes them through unchanged. Setup steps: [Remote execution](../guides/remote-execution.mdx#connect-a-provider).

| Variable | What it does |
|---|---|
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | Modal API token. Alternatively, `modal setup` stores a token in `~/.modal.toml`. |
| `DAYTONA_API_KEY` | Daytona API key. `DAYTONA_API_URL` and `DAYTONA_TARGET` override the API endpoint and target region. |

## UI / logging

Standard cross-tool env vars, honored automatically.

| Variable | What it does |
|---|---|
| `NO_COLOR` | Any non-empty value disables Rich's ANSI styling. |
| `CI` | When set, Rich auto-disables styling (assumes a non-interactive log target). |
| `TERM` | `TERM=dumb` disables styling. |

## Hub Visualiser

| Variable | What it does | Default |
|---|---|---|
| `R2E_VISUALISER_URL` | Override the base URL of the "View tasks in Harbor Visualiser" badge on dataset cards written by `repo2rlenv push`. Cards written by `repo2rlenv release` always link to the default. | `https://huggingface.co/spaces/HuggingFaceH4/harbor-visualiser` |

---

## `.env` files

Every `repo2rlenv` command loads a `.env` file at startup, using `python-dotenv`. Variables that are already set in your environment win: the file only fills in the ones that are missing. Start from the template in the repository with `cp .env.example .env`.

The search for `.env` starts in the directory of the **installed `repo2rlenv` package** and walks up through its parents. It does not start from your current working directory, so which file is found depends on how you installed the tool:

| Installation | `.env` that is found |
|---|---|
| Source checkout (`uv run repo2rlenv …`, or an editable install) | The one at the checkout root. |
| Into a virtual environment inside your project (`./.venv`) | The one in your project directory, found on the way up out of `.venv`. |
| As a global tool (`uv tool install`, `pipx`) or into a shared environment | Only a `.env` in a parent directory of that environment, such as your home directory. A `.env` in the directory you run the command from is **not** loaded. |

If your `.env` isn't picked up, export the variables in your shell (`set -a; . ./.env; set +a`) or use your CI runner's secret manager. `tasksmith run`, `tasksmith batch`, `tasksmith bootstrap` and `quality run` also accept `--env-file PATH`, which loads that exact file, again without overriding variables that are already set.

Set `PYTHON_DOTENV_DISABLED=1` to turn automatic loading off, for example in CI jobs that must see only their own secrets.
