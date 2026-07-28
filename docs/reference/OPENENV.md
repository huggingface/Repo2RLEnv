# Running Repo2RLEnv datasets on OpenEnv

[OpenEnv](https://github.com/huggingface/OpenEnv) is a Gymnasium-style standard
for containerized agentic environments — `reset()` / `step()` / `state()` over a
WebSocket, deployable as a Docker image or a Hugging Face Space.

Repo2RLEnv datasets run on it **unchanged**. OpenEnv's `harbor_env` serves Harbor
task directories directly, so a dataset built for `harbor run` is also a training
environment. There is no export step, no second copy of the data, and no
Repo2RLEnv-specific code in OpenEnv.

## Why there is nothing to convert

The two projects sit at different layers and agree on the one contract that
matters — **the reward is produced inside the environment and only forwarded**.

| Layer | Repo2RLEnv | Harbor | OpenEnv |
|---|---|---|---|
| Makes the task | ✅ synthesis pipelines | — | — |
| Defines the task format | emits it | ✅ owns it | consumes it |
| Runs a batch evaluation | — | ✅ `harbor run` | — |
| Serves an episode loop for training | — | — | ✅ `reset` / `step` / `state` |

What the runtime does with each file is identical on both sides:

| Task file | Under `harbor run` | Under `harbor_env` |
|---|---|---|
| `instruction.md` | the agent's prompt | the observation returned by `reset()` |
| working directory | what the agent edits | `step(exec / read / write)` |
| `tests/test.sh` | the verifier phase | `step(evaluate)` |
| `/logs/verifier/reward.{json,txt}` | the trial's reward | `observation.reward`, forwarded verbatim |
| `solution/solve.sh` | the oracle agent | `step(solve)` |
| `environment/Dockerfile` | the sandbox image | the sandbox image |

## Recipe

```bash
# 1. Synthesize tasks from a repository
repo2rlenv generate \
  --repo pallets/click --pipeline pr_runtime \
  --pipeline-opt limit=10 --llm anthropic/claude-sonnet-4-6 \
  --out ./tasks

# 2. Confirm every task is solvable by its own oracle before training on it
#    (from an OpenEnv checkout)
PYTHONPATH=src:envs uv run python envs/harbor_env/examples/validate_taskset.py \
  --tasks ./tasks --mode docker

# 3. Serve the task set
HARBOR_TASKS_DIR=./tasks HARBOR_MODE=docker uv run --project envs/harbor_env server
```

```python
# 4. Drive it like any OpenEnv environment
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

## Which execution mode a task needs

`harbor_env` has two backends. **Every Repo2RLEnv task needs the Docker one**,
because every pipeline puts the repository state inside the image the task
carries — that is the whole point of the `environment/Dockerfile` we emit.

| Pipeline | Emits `environment/Dockerfile` | `harbor_env` mode |
|---|:-:|---|
| `pr_runtime`, `commit_runtime`, `cve_patches` | ✅ | `docker` |
| `code_instruct`, `equivalence_tests` | ✅ | `docker` |
| `pr_diff` (default `emit_harbor_env=True`) | ✅ | `docker` |
| `pr_diff` with `emit_harbor_env=False` | ❌ (no `tests/` either) | not runnable — score the stored diff client-side with `repo2rlenv.reward.calculate_diff_similarity_reward` |

The local (Docker-free) backend exists for *self-contained* tasks that ship their
starting files in `environment/` instead of an image; it refuses image-backed
tasks with an explicit message rather than grading an empty directory. Hugging
Face Spaces cannot run Docker-in-Docker, so a Space serves only that mode — run
Repo2RLEnv task sets on a Docker-capable host.

## Rewards

Our verifiers write the files Harbor specifies, and `harbor_env` reads them in
Harbor's order — `reward.json` first, then `reward.txt`:

| Pipeline | `reward.txt` | Also written |
|---|---|---|
| `pr_runtime`, `commit_runtime`, `cve_patches` | `f2p_rate × p2p_rate` | `reward-details.json` — `resolved`, F2P/P2P counts, regressions, `parse_status` |
| `pr_diff` | 6-component `diff_similarity` | `reward-details.json` — per-component scores, weights, judge status |

`harbor_env` surfaces the sidecar as `observation.info["reward_details"]` and any
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
`/logs/verifier/reward.txt` — was run through both runtimes and scored the same:

| Runtime | No-op agent | Oracle (`solution/solve.sh`) |
|---|---|---|
| `harbor run` (Harbor 0.20) | 0.0 | 1.0 |
| OpenEnv `harbor_env`, `docker` mode | 0.0 | 1.0 |

See also [Related work](./RELATED_WORK.md).
