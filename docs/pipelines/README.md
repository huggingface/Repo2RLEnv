# Pipelines

A pipeline is a synthesis method that takes a repo and emits Harbor-shaped tasks. They share the same input shape (`GenerationInput`) and output shape (Harbor task dirs); they differ in **how** they manufacture verifiable tasks.

## Common shape

Every pipeline follows the same skeleton — only the box labelled "synthesize" varies.

```mermaid
flowchart LR
    A[Source repo<br/>+ config] --> B[Discover<br/>candidates]
    B --> C[Synthesize<br/>per pipeline]
    C --> D[QA gate]
    D -- pass --> E[Harbor task dir]
    D -- fail --> F[Skip + log reason]
    E --> G[Local dataset]
    E --> H[HF Hub<br/>+ registry.json]
```

## Status

| Pipeline | Status | Sandbox at gen | GPU helpful? | LLM at gen | Inspiration |
|---|---|:-:|:-:|:-:|---|
| [`pr_diff`](./pr_diff.md) | **shipped** | No | No | Optional | [SWE-RL](https://github.com/facebookresearch/swe-rl) |
| [`pr_runtime`](./pr_runtime.md) | **shipped** | Harbor | If repo's tests need it (ML repos) | Optional | [SWE-bench](https://github.com/SWE-bench/SWE-bench) |
| [`pr_stream`](./pr_stream.md) | **shipped** | Harbor | Same as `pr_runtime` | Optional | [SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live) + [RepoLaunch](https://github.com/microsoft/RepoLaunch) |
| [`commit_runtime`](./commit_runtime.md) | **shipped** | Harbor | If repo's tests need it | Yes | [R2E-Gym SWE-GEN](https://github.com/R2E-Gym/R2E-Gym) |
| [`mutation_bugs`](./mutation_bugs.md) | **shipped (v0.6)** | Harbor | Same as test suite | Yes | [SWE-smith](https://github.com/SWE-bench/SWE-smith) |
| [`code_instruct`](./code_instruct.md) | **shipped (v0.6)** | Harbor | Sometimes | Yes | [Magicoder](https://github.com/ise-uiuc/magicoder) |
| [`equivalence_tests`](./equivalence_tests.md) | planned | Harbor | If function uses GPU | Yes | [R2E](https://github.com/r2e-project/r2e) |
| [`cve_patches`](./cve_patches.md) | planned | Harbor | Rarely | Yes | [PatchSeeker](https://github.com/hungkien05/PatchSeeker) / CVE-Bench |
| [`refactor_synthesis`](./refactor_synthesis.md) | planned | Harbor | Rarely | Yes | RefactoringMiner |

**Sandbox column legend**: "No" = pure text manipulation, no execution. "Harbor" = we delegate to Harbor's sandbox layer (Local Docker / Modal / Daytona / E2B / Runloop). We don't maintain a parallel abstraction.

The reference repos are cloned shallowly to `references/` (gitignored).

## Reward kinds emitted

| Pipeline | `diff_similarity` | `test_execution` |
|---|:-:|:-:|
| `pr_diff` | ✅ | — |
| `pr_runtime` | ✅ | ✅ |
| `commit_runtime` | ✅ | ✅ |
| `mutation_bugs` | (oracle as diff) | ✅ |
| `code_instruct` | optional | ✅ |
| `equivalence_tests` | — | ✅ |
| `pr_stream` | ✅ | ✅ |
| `cve_patches` | ✅ | ✅ |
| `refactor_synthesis` | — | ✅ |

`diff_similarity` works without a sandbox; `test_execution` requires one.

## Adding a new pipeline

See the **[cookbook](../contributing/ADDING_A_PIPELINE.md)** for the full step-by-step walkthrough — covers the enum + Options + Pipeline class + tests + doc page, with template snippets and conventions taken from `pr_diff`.

TL;DR: every pipeline must satisfy the [`Pipeline` Protocol](../../src/repo2rlenv/pipelines/base.py):

```python
class Pipeline(Protocol):
    name: ClassVar[PipelineName]
    def __init__(self, input: GenerationInput, options: BaseModel) -> None: ...
    def run(self, out_dir: Path) -> PipelineResult: ...
```

`tests/test_pipeline_contract.py` verifies every registered pipeline conforms to the Protocol — adding a new one without finishing the registration steps will fail there.
