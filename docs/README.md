# Repo2RLEnv documentation

Three tiers, depending on what you're here to do.

## Start here

| | |
|---|---|
| **[quickstart.md](./quickstart.md)** | Install → generate your first dataset → push to HF Hub, in ~10 minutes |
| **[pipelines/](./pipelines/README.md)** | One doc per synthesis pipeline — status, when to use, inputs, oracle, inspiration repo |

## Reference

Stable contracts and module-level API.

| | |
|---|---|
| **[reference/SPEC.md](./reference/SPEC.md)** | Input contract (`GenerationInput`) + Harbor-shaped output contract |
| **[reference/API.md](./reference/API.md)** | Python API reference for the modules in `src/repo2rlenv/` |
| **[reference/AUTH.md](./reference/AUTH.md)** | GitHub auth (PAT / `gh` CLI) + HF / LLM key resolution |
| **[reference/BOOTSTRAP.md](./reference/BOOTSTRAP.md)** | v0.2 — LLM agent that iterates the per-repo Docker image until tests run |
| **[reference/AGENTS.md](./reference/AGENTS.md)** | The 22+ Harbor agent harnesses, their inputs, and how RL traces / logprobs leave the sandbox |
| **[reference/RELATED_WORK.md](./reference/RELATED_WORK.md)** | Per-pipeline provenance + adjacent papers, datasets, and frameworks (incl. recent Microsoft / NVIDIA code-RL work) |

## Contributing

For people shipping new pipelines or extending the spec.

| | |
|---|---|
| **[`../CONTRIBUTING.md`](../CONTRIBUTING.md)** | Top-level contribution guide — dev setup, PR conventions, commit style, CI expectations, release flow |
| **[contributing/ADDING_A_PIPELINE.md](./contributing/ADDING_A_PIPELINE.md)** | Step-by-step cookbook for shipping a new synthesis pipeline (enum + Options + Pipeline class + tests + doc) |

---

For the project pitch, install instructions, and the comparison vs SWE-bench / SWE-smith, see the [project README](../README.md).
