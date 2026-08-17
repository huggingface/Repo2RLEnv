# Authentication

GitHub auth is the only thing Repo2RLEnv really *cares* about — every pipeline starts by cloning a repo or hitting the GitHub API. The other tokens (HF, LLM, E2B) are passthroughs to upstream SDKs that already auto-resolve them.

This doc focuses on **GitHub**, then covers the others briefly.

## GitHub auth — three valid paths

Repo2RLEnv shells out to `gh` for clone + PR-list operations. `gh` itself respects `GH_TOKEN` / `GITHUB_TOKEN` env vars **above** any keychain creds, which is what makes the three paths below interchangeable.

### Path A — `gh auth login` (most ergonomic)

```bash
gh auth login
```

Stores creds in macOS Keychain (or `~/.config/gh/` on Linux). After this, `gh` is authenticated for both public and private repos. Repo2RLEnv resolves the token via `gh auth token`. **No env-var setup needed** — what most HF / OSS contributors already have.

### Path B — A read-scoped Personal Access Token (no `gh auth login` required)

If you'd rather not run `gh auth login`, generate a fine-grained PAT at <https://github.com/settings/tokens?type=beta> with **Contents: Read** scope on the repos you care about. Then either:

```bash
# In your shell or a .env file at project root
export GITHUB_TOKEN=ghp_xxx
```

This works because `gh` reads `GH_TOKEN` / `GITHUB_TOKEN` env over its keychain. You never need to log in interactively.

### Path C — Explicit per-repo env var

If you have multiple tokens (different orgs, different scopes) and want to be explicit, set the env-var name in your config:

```yaml
repo:
  url: "myorg/private-repo"
  access: "private"
  auth_token_env: "MY_ORG_PAT"
```

Then `export MY_ORG_PAT=ghp_xxx`. Repo2RLEnv reads the **name**, never the value.

## Resolution order

First match wins:

1. `repo.auth_token_env` (if explicitly set in config)
2. `gh auth token` (if `gh` is on PATH and `auth.use_gh_cli=true`, default)
3. `$GITHUB_TOKEN`
4. None — anonymous clone (fails with a clear error if `access="private"`)

Implementation: [`src/repo2rlenv/auth.py:resolve_github_token`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/auth.py).

## Input sources (GitHub · GitLab · local)

`--repo` accepts more than a GitHub `owner/name`:

| Input | Source | Token | Notes |
|---|---|---|---|
| `owner/name` or `https://github.com/...` | GitHub | `resolve_github_token` chain above | unchanged — the default |
| `https://gitlab.com/owner/name` | GitLab | `repo.auth_token_env` → `$GITLAB_TOKEN` (public needs none) | clone via `oauth2:<token>@` |
| `/abs/path`, `./rel`, `~/x`, `file://…` | Local | none (no `gh` shell-out) | canonicalized to `file://<abspath>` |

Source-aware resolution lives in [`auth.py:resolve_repo_token`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/auth.py); detection + capabilities in [`sources.py`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/sources.py).

**Capability gating.** Each source declares which platform data it can serve (`pull_requests`, `issues`, `commit_api`); each pipeline declares what it requires. `generate` blocks an incompatible combo up front. In practice: the git/source pipelines (`commit_runtime`, `code_instruct`, `equivalence_tests`) run on **any** source; `pr_diff` / `pr_runtime` mine pull/merge requests so they run on **GitHub or GitLab** (gitlab.com merge requests via the REST API); `cve_patches` is GitHub-only (OSV → github.com fix-commits + the GitHub commit API).

## Private repos at task **build** time

Generation only needs the token to fetch PR metadata + diffs via `gh` (resolved above). But two pipelines also clone the repo themselves *inside their Docker image* at build time, rather than starting `FROM` an already-cloned bootstrap image: `pr_diff` (no bootstrap at all — the env is built fresh at generation time) and `env_setup` (the bootstrap image is deliberately thrown away, so the emitted `environment/Dockerfile` clones the bare repo itself). The `_runtime` family (`pr_runtime`, `pr_to_env`, `commit_runtime`, `cve_patches` via `pr_runtime.build_environment_dockerfile`) is different: their `environment/Dockerfile` is `FROM <bootstrap_image>` — the clone already happened at bootstrap time, host-side, using the token-resolution chain above, and got baked into the committed image; the only build-time git operation is a `git fetch`/`reset` to the task's `base_commit` against the `origin` remote the bootstrap step already configured, which needs no new build arg.

For the two pipelines that *do* clone at build time, private source repos need the consumer building the image to supply a token as a Docker build arg:

```bash
harbor run -p ./datasets/<private-dataset> -a oracle --env docker \
  --build-arg GITHUB_TOKEN=$GITHUB_TOKEN      # or GIT_TOKEN for env_setup — see below
```

The emitted Dockerfile declares an `ARG` (empty default) that is injected into the clone URL right after the scheme, then the remote is reset to the clean URL immediately after cloning — so the token never persists in `git config` inside the image and is never baked into a layer. **Public** repos need no build arg at all; the clone falls back to the anonymous URL.

The build-arg **name** and the credential **shape** both depend on the pipeline and the host:

| Pipeline | Build-arg name | github.com | gitlab.com |
|---|---|---|---|
| `pr_diff` | `GITHUB_TOKEN` (fixed, for backward compatibility — its emitted Dockerfile predates GitLab support and stays byte-identical for github.com) | `https://x-access-token:${GITHUB_TOKEN}@github.com/...` | `https://oauth2:${GITHUB_TOKEN}@gitlab.com/...` |
| `env_setup` | `GIT_TOKEN` (source-neutral — it isn't a GitHub-only pipeline) | `https://x-access-token:${GIT_TOKEN}@github.com/...` | `https://oauth2:${GIT_TOKEN}@gitlab.com/...` |

Both forms are built by the same shared helper, `authed_clone_url(repo_url, arg_name=...)` in `pipelines/_eval_script.py` — it recognizes `github.com` (`x-access-token:` username) and `gitlab.com` (`oauth2:` username) and injects whichever build-arg name its caller passes; a URL matching neither host is returned unchanged. `pr_diff` originally hardcoded a GitHub-only `str.replace`, which silently cloned GitLab MR-mined repos *unauthenticated* on the private-repo path — `authed_clone_url` is what fixed that without changing `pr_diff`'s GitHub behavior.

The same generation-time token also packages bootstrap-built images for `_runtime` pipelines (cloned host-side and `docker cp`'d in — never embedded). See [`reference/BOOTSTRAP.md`](./BOOTSTRAP.md).

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `gh CLI not found on PATH` | `gh` not installed | `brew install gh` |
| `gh auth list` reports "not logged in", env empty, public repo | No token resolved at all | `gh auth login` OR `export GITHUB_TOKEN=...` |
| `401 Unauthorized` on private repo | Token has wrong scope | Regenerate PAT with `Contents: Read` on the repo |
| `404 Not Found` on private repo | Token doesn't have access OR repo doesn't exist | Confirm via `gh repo view <owner>/<name>` |

## Other services (brief)

These are passthroughs — Repo2RLEnv reads them but defers to the upstream SDK's resolution.

### Hugging Face Hub

`huggingface_hub` auto-resolves a token from `~/.cache/huggingface/token` (set by `huggingface-cli login`) **or** the `HF_TOKEN` env var. We don't override either default. For private dataset push, the token needs **write** scope on the namespace.

### LLM providers

LiteLLM resolves provider keys from provider-default env vars:

| Provider | Env var |
|---|---|
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Hugging Face Router | `HF_TOKEN` |
| Together | `TOGETHER_API_KEY` |
| Groq | `GROQ_API_KEY` |

Override with `llm.api_key_env` in your config if you have non-default names.

### Container registry (image distribution for `_runtime` datasets)

`repo2rlenv push` distributes bootstrap images to an OCI registry when a dataset ships `environment/Dockerfile`s. Creds resolve from explicit env vars first:

| Registry | Env vars | Notes |
|---|---|---|
| GHCR | `GHCR_TOKEN` or `GITHUB_TOKEN` | one-time `gh auth refresh -h github.com -s write:packages` |
| Docker Hub | `DOCKER_USERNAME` + `DOCKER_TOKEN` (PAT) | preferred over the credstore (whose OAuth token is often pull-only); pushes under the Docker Hub user's namespace |

If no registry verifies, push falls back to **inline mode** — each task bakes its own rebuild recipe and stays reproducible with no registry at all. Full details: [`reference/REGISTRY_AUTH.md`](./REGISTRY_AUTH.md).

### E2B

If you use Harbor with the E2B provider (`harbor run -d <dataset> -e e2b ...`), the E2B SDK reads `E2B_API_KEY` from env. Repo2RLEnv itself doesn't run E2B — Harbor does.

## What's never stored

- Token *values* are never written to task directories, the lockfile, git, or logs
- Container registry credentials are sandbox-side (`docker login`, IAM roles)
- Verifier-time secrets are forbidden by the spec — a task that needs a paid API key to run is non-conformant
