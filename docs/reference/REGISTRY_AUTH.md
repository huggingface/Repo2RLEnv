# Container registry authentication

`repo2rlenv push` (v0.8.2.post3+) uploads the bootstrap Docker image to an
OCI registry alongside the HF Hub dataset so any consumer can `harbor run`
the published tasks. This page covers how to log into each supported
registry and verify the credentials work.

Run `repo2rlenv push --check-auth` to probe every detected registry and
get a one-shot report. No image is pushed, no garbage created.

## Default flow

You don't have to pre-configure anything — `repo2rlenv push` auto-detects
the first verified registry from `~/.docker/config.json`. If nothing is
logged in, push falls back to inline-Dockerfile mode (recipe-level
reproducibility) and warns about how to upgrade.

For the launch / CI path use `--require-registry` to hard-fail rather
than fall back silently.

## GHCR (default recommendation)

Free for public images, no anonymous-pull rate limit, ties naturally to a
GitHub org. **Recommended default for new datasets.**

```bash
gh auth refresh -h github.com -s write:packages
echo "$(gh auth token)" | docker login ghcr.io \
  -u "$(gh api user --jq .login)" --password-stdin
```

`repo2rlenv push` will pick `ghcr.io/<hf-dataset-owner>/...` automatically.
If you lack `write:packages` on that GitHub org, push falls back to
`ghcr.io/<gh-login-user>/...`. Repository visibility is flipped to public
via the GitHub API (`PATCH /user|orgs/packages/container/<name>`) when the
HF dataset is public.

## AWS ECR Private

Token TTL is 12h — install `amazon-ecr-credential-helper` once and forget
about it.

```bash
brew install docker-credential-helper-ecr  # or apt
# Add to ~/.docker/config.json:
#   "credHelpers": { "<acct>.dkr.ecr.<region>.amazonaws.com": "ecr-login" }
```

Or one-off (re-run every 12h):

```bash
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin \
    <acct>.dkr.ecr.us-east-1.amazonaws.com
```

ECR requires the repository to pre-exist; `repo2rlenv push` creates it
automatically via `aws ecr create-repository` if the L4 probe returns 404.
Make sure your IAM role has `ecr:CreateRepository`.

## AWS ECR Public

Distinct service from Private. **50 GB free public storage, forever.**

```bash
aws ecr-public get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin public.ecr.aws
```

Or use `amazon-ecr-credential-helper` against `public.ecr.aws`.

## Azure Container Registry

Token TTL is 3h.

```bash
az login                              # one-time
az acr login --name <myregistry>      # per session
```

For anonymous public pulls (Standard tier or higher):
`az acr update -n <reg> --anonymous-pull-enabled true`.

## GCP Artifact Registry

`gcloud` registers itself as a Docker credential helper per region:

```bash
gcloud auth login
gcloud auth configure-docker us-central1-docker.pkg.dev
```

Repository must pre-exist; `repo2rlenv push` creates it via
`gcloud artifacts repositories create` if the L4 probe returns 404.
For public pulls:

```bash
gcloud artifacts repositories add-iam-policy-binding <repo> \
  --location=us-central1 --member=allUsers \
  --role=roles/artifactregistry.reader
```

## Docker Hub

Default tier has a **100 pulls / 6h / IP anonymous limit** — fine for
small datasets, but a real problem for benchmarks pulled by many
consumers or in CI. `repo2rlenv push` will NOT pick Docker Hub as the
auto-default when other registries are available; pass `--image-registry
index.docker.io/<user>` to force it.

```bash
docker login                          # interactive PAT prompt
# OR
echo "$DOCKER_TOKEN" | docker login -u "$DOCKER_USERNAME" --password-stdin
```

**Credential resolution.** For Docker Hub the probe prefers the explicit
env vars **`DOCKER_USERNAME` + `DOCKER_TOKEN`** (a PAT) over the docker
credstore — the credstore returns an OAuth *identity token* that is often
pull-only at the token endpoint, while an explicit PAT reliably grants
push. The push **namespace** is the authenticated Docker Hub user
(`DOCKER_USERNAME`), not the HF dataset owner, so images land under
`index.docker.io/<DOCKER_USERNAME>/…`. (GHCR analogously reads
`GHCR_TOKEN` / `GITHUB_TOKEN`.)

**Multi-repo datasets** (e.g. `pr_runtime` across many repos, with one
bootstrap image per repo) are fully supported: `push` pushes **each**
distinct image and rewrites **each** task's `environment/Dockerfile` to
its own registry digest. If any push fails (or no registry is verified),
it falls back to inline mode — each task bakes its own rebuild recipe and
stays reproducible with no registry at all.

## Local / `registry:2` (testing only)

For development tests that need a real OCI registry but don't want any
cloud signup:

```bash
docker run -d -p 5000:5000 --name r2e-test-reg registry:2
repo2rlenv push ./datasets/mydataset owner/repo \
  --image-registry localhost:5000
```

The probe correctly classifies localhost and skips L2 auth (the default
registry:2 image runs without auth). For an authenticated local registry,
see the upstream docs at <https://distribution.github.io/distribution>.

## Anonymous-ephemeral: `ttl.sh`

For one-shot end-to-end smoke tests with no signup:

```bash
repo2rlenv push ./datasets/mydataset owner/repo \
  --image-registry ttl.sh
```

Images auto-delete after 1h (configurable in the tag).

## What `--check-auth` actually probes

For each registry the L1–L4 protocol runs in order:

| Level | Endpoint | What it confirms |
|---|---|---|
| L1 | `GET /v2/` | Registry reachable |
| L2 | Bearer-token exchange | Credentials parse + registry accepts them |
| L3 | `HEAD /v2/<ns>/r2e-bootstrap-probe/manifests/latest` (404 = pass) | Read access on target namespace |
| L4 | `POST /v2/<ns>/r2e-bootstrap-probe/blobs/uploads/` + `DELETE` cancel | Write access |

L4 leaves no image, layer, or session artifact. Total wire time per
registry: ~200–500 ms. `--fast` stops at L2 if you just want a quick
sanity check.
