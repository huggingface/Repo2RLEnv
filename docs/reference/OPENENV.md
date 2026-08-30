# Running Repo2RLEnv datasets on OpenEnv

[OpenEnv](https://github.com/huggingface/OpenEnv) is a Gymnasium-style standard
for containerized agentic environments — `reset()` / `step()` / `state()` over a
WebSocket, deployable as a Docker image or a Hugging Face Space.

Repo2RLEnv datasets run on it with **no change to the task data**. There are two
ways to get there, and both serve the exact directories `generate` emitted:

| | What it is | Use it when |
|---|---|---|
| **`repo2rlenv export --format openenv`** | We emit a deployable environment package around your dataset — `Dockerfile`, `openenv.yaml`, Space card, server. Runs on the `repo2rlenv[openenv]` runtime. | You want a standalone image or a Hugging Face Space, with no OpenEnv checkout involved. |
| **OpenEnv's `harbor_env`** | OpenEnv's own generic runtime for Harbor task directories. Point it at your dataset. | You already work inside an OpenEnv checkout, or you mix our tasks with other Harbor producers. |

Either way the reward comes from your task's own `tests/test.sh` and is only
forwarded. Neither path rewrites, re-authors, or copies the task format.

## Quick start — export a deployable environment

```bash
# 1. Synthesize tasks from a repository
repo2rlenv generate --repo pallets/click --pipeline pr_runtime \
  --pipeline-opt limit=10 --llm anthropic/claude-sonnet-4-6 --out ./tasks

# 2. Wrap them in an OpenEnv environment
pip install 'repo2rlenv[openenv]'
repo2rlenv export --format openenv ./tasks --out ./click-env

# 3. Run it. Task containers start on the host daemon, so mount the socket.
docker build -t click-env ./click-env
docker run --rm -p 8000:8000 -v /var/run/docker.sock:/var/run/docker.sock click-env
```

```python
import asyncio
from repo2rlenv.openenv import Repo2RLEnvClient

async def main():
    env = Repo2RLEnvClient(base_url="http://localhost:8000")
    start = await env.reset(task_id="pallets__click-2951")
    print(start.observation.instruction)                 # instruction.md

    await env.write_file("src/click/core.py", patched)   # the agent's edit
    result = await env.evaluate()                        # runs tests/test.sh

    print(result.reward)                                 # the verifier's score
    print(result.observation.info["reward_details"])     # F2P/P2P breakdown
    await env.close()

asyncio.run(main())
```

The emitted package is thin — `server/app.py` is two lines over
`repo2rlenv.openenv.build_app`, and `tasks/` holds your task directories byte for
byte. The runtime lives in `repo2rlenv.openenv`, so it is versioned, tested and
upgraded like the rest of the library rather than generated into your output.

`export` writes `README.md` with Hugging Face Space front-matter (`sdk: docker`),
so the emitted directory can be pushed straight to a Space.

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--out` | `<dataset>-openenv` | Where to write the package |
| `--name` | dataset directory name | Environment name in `openenv.yaml` and the Space card |
| `--requirement` | `repo2rlenv[openenv]>=<installed>` | The requirement the image installs — pin a release, a git ref, or a local wheel |
| `--base-image` | `python:3.12-slim` | Base image for the server |
| `--port` | `8000` | Port the server listens on |

## Why there is nothing to convert

The projects sit at different layers and agree on the one contract that
matters — **the reward is produced inside the environment and only forwarded**.

| Layer | Repo2RLEnv | Harbor | OpenEnv |
|---|---|---|---|
| Makes the task | ✅ synthesis pipelines | — | — |
| Defines the task format | emits it | ✅ owns it | consumes it |
| Runs a batch evaluation | — | ✅ `harbor run` | — |
| Serves an episode loop for training | ✅ `export --format openenv` | — | ✅ `reset` / `step` / `state` |

What the runtime does with each file is identical on both sides:

| Task file | Under `harbor run` | Under an OpenEnv runtime |
|---|---|---|
| `instruction.md` | the agent's prompt | the observation returned by `reset()` |
| working directory | what the agent edits | `step(exec / read / write)` |
| `tests/test.sh` | the verifier phase | `step(evaluate)` |
| `/logs/verifier/reward.{json,txt}` | the trial's reward | `observation.reward`, forwarded verbatim |
| `solution/solve.sh` | the oracle agent | `step(solve)` |
| `environment/Dockerfile` | the sandbox image | the sandbox image |

## Serving with OpenEnv's `harbor_env` instead

OpenEnv ships `harbor_env`, a generic runtime for any Harbor task directory. It
needs no export step at all — point it at the dataset:

```bash
# 1. Confirm every task is solvable by its own oracle before training on it
#    (from an OpenEnv checkout; `--with docker` supplies the Docker SDK)
PYTHONPATH=src:envs uv run --with docker python envs/harbor_env/examples/validate_taskset.py \
  --tasks ./tasks --mode docker

# 2. Serve the task set
HARBOR_TASKS_DIR=./tasks HARBOR_MODE=docker uv run --project envs/harbor_env server
```

```python
# 3. Drive it like any OpenEnv environment
import asyncio
from harbor_env import HarborEnv

async def main():
    env = HarborEnv(base_url="http://localhost:8000")
    start = await env.reset(task_id="pallets__click-2951")
    print(start.observation.instruction)                 # instruction.md

    await env.write_file("src/click/core.py", patched)   # the agent's edit
    result = await env.evaluate()                        # runs tests/test.sh

    print(result.reward)                                 # the verifier's score
    print(result.observation.info["reward_details"])     # F2P/P2P breakdown
    await env.close()

asyncio.run(main())
```

Datasets published with `repo2rlenv push` keep their tasks under `tasks/<id>/`;
that layout is discovered automatically, so a Hub dataset can be served directly:

```bash
HARBOR_TASKS_DIR=hf://datasets/<owner>/<name> HARBOR_MODE=docker \
  uv run --project envs/harbor_env server
```

## Which tasks can actually run

**Every runtime task needs Docker**, because every pipeline puts the repository
state inside the image the task carries — that is the whole point of the
`environment/Dockerfile` we emit.

| Pipeline | Emits `environment/Dockerfile` | Runnable |
|---|:-:|---|
| `pr_runtime`, `commit_runtime`, `cve_patches` | ✅ | yes, Docker |
| `code_instruct`, `equivalence_tests` | ✅ | yes, Docker |
| `pr_diff` (default `emit_harbor_env=True`) | ✅ | yes, Docker |
| `pr_diff` with `emit_harbor_env=False` | ❌ (no `tests/` either) | no — score the stored diff client-side with `repo2rlenv.reward.calculate_diff_similarity_reward` |

`repo2rlenv export` reports how many of the bundled tasks are runnable and says
so in the emitted Space card; the runtime refuses a task with no image and no
verifier rather than grading an empty directory. `harbor_env`'s Docker-free
`local` backend is for *self-contained* tasks that ship their starting files in
`environment/`, which is not the shape any of our pipelines emit.

Because a Hugging Face Space cannot run Docker-in-Docker, a Space built from
`export` serves the API but cannot start task containers — run Repo2RLEnv task
sets on a Docker-capable host, or use a Space only as a front end.

When a dataset has been published with `repo2rlenv push`,
`[metadata.repo2env.reproducibility]` records `mode = "registry"` and a pullable
`image_ref`. Our runtime pulls that image instead of rebuilding the Dockerfile,
so a pushed dataset starts episodes without a build step.

## Rewards

Our verifiers write the files Harbor specifies, and both runtimes read them in
Harbor's order — `reward.json` first, then `reward.txt`:

| Pipeline | `reward.txt` | Also written |
|---|---|---|
| `pr_runtime`, `commit_runtime`, `cve_patches` | `f2p_rate × p2p_rate` | `reward-details.json` — `resolved`, F2P/P2P counts, regressions, `parse_status` |
| `pr_diff` | 6-component `diff_similarity` | `reward-details.json` — per-component scores, weights, judge status |

Both runtimes surface the sidecar as `observation.info["reward_details"]` and any
flat metrics from `reward.json` as `observation.info["reward_metrics"]`, so the
training signal and the diagnostic breakdown both survive the trip.

A verifier that writes no reward file produces **no reward** on the OpenEnv side
(`reward=None` plus an explicit error), not a `0.0` — the same rule that makes
our `reward.txt` contract trustworthy in the first place.

See [SPEC](./SPEC.md) and [REWARD_SCHEMA](./REWARD_SCHEMA.md) for the full
emitted contract.

## Verified

The task-directory shape we emit — `version = "1.0"`, `[metadata.repo2env]`,
`environment/Dockerfile` with `WORKDIR /workspace`, a `tests/test.sh` writing
`/logs/verifier/reward.txt` — was built with our own emitter and run through all
three runtimes. They agree:

| Runtime | No-op agent | Oracle (`solution/solve.sh`) |
|---|---|---|
| `harbor run` (Harbor 0.20.0) | 0.0 | 1.0 |
| `repo2rlenv export --format openenv` | 0.0 | 1.0 |
| OpenEnv `harbor_env`, `docker` mode | 0.0 | 1.0 |

On the OpenEnv side the working directory resolves to `/workspace` from the
image, the scalar is read from `reward.txt`, and `reward-details.json` arrives
intact as `observation.info["reward_details"]` (F2P/P2P counts, `resolved`,
`parse_status`).

The exported environment was verified as a *built container*, not just in
process: `docker build` on the emitted Dockerfile, run with the host Docker
socket mounted, then driven over the WebSocket API — `reset` → `write_file` →
`evaluate` — taking the same task from 0.0 to 1.0 through an agent edit rather
than the oracle.

## Where the code lives

| Piece | Module |
|---|---|
| Task emission (Harbor format) | `repo2rlenv.emitter.harbor` |
| Environment emission (OpenEnv package) | `repo2rlenv.emitter.openenv` |
| Reading an emitted dataset | `repo2rlenv.openenv.dataset` |
| Reward-file contract | `repo2rlenv.openenv.reward` |
| Docker sandbox | `repo2rlenv.openenv.sandbox` |
| Gymnasium environment | `repo2rlenv.openenv.environment` |
| Trainer-facing client | `repo2rlenv.openenv.Repo2RLEnvClient` |

Only the serving modules need the extra; `repo2rlenv.openenv.dataset` and
`repo2rlenv.emitter.openenv` work with a plain `pip install repo2rlenv`, so
`export` runs without pulling in the OpenEnv stack.

See also [Related work](./RELATED_WORK.md).
