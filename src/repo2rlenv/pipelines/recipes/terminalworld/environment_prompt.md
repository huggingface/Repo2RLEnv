# Step 1: Write the Dockerfile

## 🎯 High-Level Objective
The goal of Step 1 is to read the provided signals — `env_signals.json`, `solve.sh`, and any referenced source repositories — and directly produce a working `Dockerfile` (and optional `docker-compose.yaml`). There is no intermediate spec file. You write the Dockerfile yourself based on what you observe.

## 📥 Inputs
- `<OUTPUT_DIR>/env_signals.json` — structured environment signals (OS hints, special cases, external URLs).
- `<TASK_DIR>/solution/solve.sh` — the target command sequence. Read this to understand what tools, runtimes, and dependencies must be present.

## ⚙️ Execution Phases

Before proceeding, ensure the output directory exists: `mkdir -p <OUTPUT_DIR>`.

### Phase 1: Read the Signals

Read both input files:

```bash
# Read env_signals.json for OS hints, special cases, external URLs
cat <OUTPUT_DIR>/env_signals.json

# Read solve.sh to understand what the environment must support
cat <TASK_DIR>/solution/solve.sh
```

From these, identify:
- **Base image**: infer from OS/distro hints, or from which package manager the commands use (`apt` → ubuntu/debian, `yum`/`dnf` → centos/fedora, `apk` → alpine). Default to `ubuntu:22.04` if unclear.
- **Language runtimes**: infer from binaries invoked (`python3`, `node`, `go`, `rustc`, etc.) and any version hints in the commands.
- **System packages**: collect all `apt-get install`, `yum install`, etc. lines from solve.sh and env_signals. Also infer implicit C-library dependencies from Python/Ruby/Node packages being installed (e.g. `psycopg2` → `libpq-dev`).
- **Services**: infer from connection strings or explicit service startup commands in solve.sh.
- **Working directory**: infer from the first `cd` in solve.sh, or the repo name if a repo is cloned.
- **Repos to clone**: extract git clone URLs from solve.sh or `external_urls` in env_signals.
- **Input file dependencies**: files that solve.sh reads/opens but does not itself create. Common signals: `cat <file>`, `< <file>`, `open("<file>", "r")`, `pd.read_csv(...)`. For each such file, check whether the Dockerfile already provides it. If not, resolve it in Phase 3.

### Phase 2: Scan Source Repositories (If Applicable)

If solve.sh clones repositories, or if `external_urls` in env_signals points to source repos, scan them to gather objective facts before writing the Dockerfile. This prevents guessing hidden dependencies.

```bash
# Clone each repository
git clone --depth 1 <URL> <OUTPUT_DIR>/repos/<reponame>

# Scan for project structure and dependency files
python3 .claude/skills/docker-env-builder/scripts/detect_project.py \
    --repo-dir <OUTPUT_DIR>/repos/<reponame> \
    --output-file <OUTPUT_DIR>/project_detection_<reponame>.json
```

Use `files_found` in the scan output to read key manifests (`requirements.txt`, `package.json`, `go.mod`, etc.) and any existing `Dockerfile` or `docker-compose.yaml` the repo contains. Extract knowledge from them:

- **Existing Compose files**: read them for exact service names, environment variables, and database credentials. Prefer using the repo's service definitions over writing from scratch.
- **Existing Dockerfiles**: extract `apt-get` packages, `ENV` variables, and setup steps. Do NOT blindly copy production Dockerfiles (they are often stripped of tools needed for replay). Extract and inject relevant pieces into your Dockerfile.

### Phase 3: Write the Dockerfile

Write `<OUTPUT_DIR>/Dockerfile` directly. Base your decisions on everything you read in Phases 1–2.

**Dockerfile rules:**
- **Environment only**: install packages, configure env vars, clone repos, install project dependencies. Do NOT run user business logic inside `RUN` layers.
- **No multi-stage builds** that discard source code.
- **Fat dev container**: include all tools needed to replay solve.sh commands (e.g. `git`, `curl`, build tools). Do not optimize for image size.
- Use `WORKDIR` to set the working directory inferred from solve.sh.

If services are required, write `<OUTPUT_DIR>/docker-compose.yaml` as well.

**Resolving missing input files:** For each input dependency identified in Phase 1 that the Dockerfile does not yet provide, follow this priority order:

1. **Download** — check `env_signals.json` (`external_urls`), `solve.sh`, and `source/info.json` for `curl`/`wget`/`git clone` URLs that fetch this file. If a live URL exists, add a `RUN curl -o ...` or `RUN wget ...` step to the Dockerfile.
2. **Synthesize** — if no URL is available or reliable: infer the file's content and structure from how solve.sh uses it (argument patterns, field references, processing logic) and from `source/info.json`. Create the file in `<OUTPUT_DIR>/` and add a `COPY` step with a comment:

```dockerfile
# synthesized: data.csv not available from source; inferred from solve.sh
COPY data.csv /app/data.csv
```

Only read `source/recording.txt` if solve.sh and info.json are insufficient to infer the file's structure.

Synthesized content must be representative enough that the task remains meaningful — not trivially empty.

If the environment uses a framework or toolchain you are unfamiliar with, use WebSearch before writing: `"<framework> dockerfile setup dependencies"`.

Load only the references relevant to your decisions — **do not read all reference files upfront**:

| Situation | Reference |
|---|---|
| Dockerfile patterns and best practices | [references/dockerfile_best_practices.md](../references/dockerfile_best_practices.md) |
| Multi-service compose setups | [references/compose_best_practices.md](../references/compose_best_practices.md) |
| Build failure diagnosis patterns | [references/build_failure_diagnostics.md](../references/build_failure_diagnostics.md) |

### Phase 4: Lint and Validate

Before attempting a build, run the linter to catch common mistakes:

```bash
# Lint Dockerfile
# Outputs JSON: {"has_errors": bool, "errors": [...]}
python3 .claude/skills/docker-env-builder/scripts/lint_dockerfile.py <OUTPUT_DIR>/Dockerfile

# If a compose file was written, validate its syntax
cd <OUTPUT_DIR> && \
  if ls *compose.y*ml 1> /dev/null 2>&1; then docker compose config --quiet && echo "Compose syntax OK" || echo "Compose syntax error — fix before building"; fi
```

If `"has_errors": true` or the Compose validation fails, fix the issues and re-lint before proceeding.

## 📤 Output

| File | Always written | Description |
|---|---|---|
| `Dockerfile` | Yes | Primary build artifact |
| `docker-compose.yaml` | Only if services required | Multi-service orchestration |

---
**Next Step:** After the Dockerfile passes linting, proceed to [STEP 2](STEP2.md).
