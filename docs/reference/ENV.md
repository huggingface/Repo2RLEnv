# Environment variables

Every variable Repo2RLEnv reads, in one place. None of them are required to *use* the tool — sensible defaults exist for all of them — but you'll want to know what's available when wiring up CI, Docker images, or a cron host.

Variables are grouped by what they affect.

## Storage paths

| Variable | What it controls | Default |
|---|---|---|
| `R2E_CACHE_DIR` | Bootstrap image cache root — where the LLM-built per-repo Docker images are stored, keyed by content hash. The expensive step runs once per (repo, ref); subsequent generations reuse the cache. | `./workspace/bootstrap` |

The CLI flag `--cache-dir` takes precedence over the env var, and the env var takes precedence over the default — standard layering.

> The dataset output path (`--out`) and any project-local state are intentionally **not** env-controlled: those are per-invocation choices that should live in your generate command or `Makefile`, not in shell state.

## GitHub auth

Used by every pipeline (mining + cloning).

| Variable | What it does |
|---|---|
| `GITHUB_TOKEN` *(or `GH_TOKEN`)* | Personal access token. Read in the **third** position of the auth chain — after an explicitly-named token (`repo.auth_token_env` in your config) and after `gh auth token` if `gh` is logged in. |
| `repo.auth_token_env` *(config field, not env)* | Names which env var holds the token for *this* repo (useful when you have multiple org-scoped tokens). The token *value* is never embedded in config — only the *name*. |

Full resolution order + private-repo build-arg flow: [`AUTH.md`](./AUTH.md).

## Hugging Face Hub

For `repo2rlenv push` / `pull`. Resolved by `huggingface_hub` itself; we don't override.

| Variable | What it does |
|---|---|
| `HF_TOKEN` | Hub access token. Alternatively, `huggingface-cli login` writes it to `~/.cache/huggingface/token` and the SDK picks it up automatically. Push needs **write** scope on the target namespace. |

## LLM providers

LiteLLM-resolved; per-provider defaults. Override with `llm.api_key_env` in config if you use non-default names.

| Variable | Provider |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic (Claude) |
| `OPENAI_API_KEY` | OpenAI |
| `HF_TOKEN` | Hugging Face Router |
| `TOGETHER_API_KEY` | Together |
| `GROQ_API_KEY` | Groq |

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

The diff-similarity verifier baked into every `pr_diff` task is configurable at *score time* without rebuilding the image — the verifier reads these inside the container.

| Variable | What it does | Default |
|---|---|---|
| `R2E_W_FORMAT` | Weight for the *format-valid* component (does the diff parse?). | wired in source |
| `R2E_W_SIZE` | Weight for the *size sanity* component. | wired in source |
| `R2E_W_FILE` | Weight for the *file-targeting* component (F1 over touched files). | wired in source |
| `R2E_W_REGION` | Weight for the *region overlap* component. | wired in source |
| `R2E_W_SIM` | Weight for the *changes-only similarity* component. | wired in source |
| `R2E_W_JUDGE` | Weight for the *LLM-as-judge* semantic-correctness component. | wired in source |
| `R2E_JUDGE_MODEL` | Override the judge model (LiteLLM-qualified name). | claude-haiku |
| `ANTHROPIC_API_KEY` | Required for the LLM-judge component; the verifier degrades gracefully (records `status=no_api_key`) when unset, so the other five components still score. |

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
| `HARBOR_VISUALISER_BASE_URL` | Override the dataset-card "View tasks in Visualiser" badge URL. | the official Harbor Visualiser Space |

---

## A note on `.env` files

Repo2RLEnv does not auto-load `.env` files. Use your shell, your CI runner's secret manager, or a wrapper like `direnv` / `set -a; . .env; set +a`. We keep the loading explicit on purpose — silent dotenv loading hides which token is being read where.
