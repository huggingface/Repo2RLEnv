# Related Work & Provenance

Repo2RLEnv stands on a growing body of research into turning real software
repositories into executable, verifiable environments for training and
evaluating code agents. This page records two things:

1. **Provenance** — exactly what each shipped pipeline draws inspiration from
   (these mirror the `Acknowledgment` blocks in the pipeline source files).
2. **Related work & other implementations** — adjacent papers, datasets, and
   frameworks worth knowing, including recent model lines from Microsoft and
   NVIDIA that consume verifiable code-RL environments.

> No code is copied from any source below. Every implementation is independent
> and Apache-2.0 licensed. The one non-permissive influence is SWE-RL (CC BY-NC
> 4.0); our `reward.py` is a clean reimplementation of the *concept* and carries
> an explicit license-posture note.

## Provenance — what each pipeline draws from

| Pipeline | Inspiration | Paper | Repo (license) |
|---|---|---|---|
| `pr_diff` | SWE-RL | *SWE-RL* (Wei et al., NeurIPS '25) — [arXiv:2502.18449](https://arxiv.org/abs/2502.18449) | [facebookresearch/swe-rl](https://github.com/facebookresearch/swe-rl) (CC BY-NC 4.0) · [SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench) (MIT) |
| `pr_runtime` | SWE-bench + SWE-bench-Live | *SWE-bench* (Jimenez et al., ICLR '24) — [arXiv:2310.06770](https://arxiv.org/abs/2310.06770); *SWE-bench-Live* (Zhang et al., NeurIPS '25) — [arXiv:2505.23419](https://arxiv.org/abs/2505.23419) | [SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench) · [microsoft/SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live) (both MIT) |
| `commit_runtime` | R2E-Gym ("SWE-GEN" commit mining) | *R2E-Gym* (Jain et al., COLM '25) — [arXiv:2504.07164](https://arxiv.org/abs/2504.07164) | [R2E-Gym/R2E-Gym](https://github.com/R2E-Gym/R2E-Gym) (MIT) |
| `cve_patches` | PatchSeeker + CVE-Bench + OSV | *PatchSeeker* (Le et al.); *CVE-Bench* (Zhu et al., NAACL '25) | [hungkien05/PatchSeeker](https://github.com/hungkien05/PatchSeeker) (MIT) · [OSV API](https://osv.dev) |
| `mutation_bugs` | SWE-smith | *SWE-smith* (Yang et al., NeurIPS '25 Spotlight) — [arXiv:2504.21798](https://arxiv.org/abs/2504.21798) | [SWE-bench/SWE-smith](https://github.com/SWE-bench/SWE-smith) (MIT) |
| `code_instruct` | Magicoder / OSS-Instruct | *Magicoder* (Wei et al., ICML '24) — [arXiv:2312.02120](https://arxiv.org/abs/2312.02120) | [ise-uiuc/magicoder](https://github.com/ise-uiuc/magicoder) (MIT) |
| `equivalence_tests` | R2E | *R2E* (Jain et al., ICML '24) | [r2e-project/r2e](https://github.com/r2e-project/r2e) (MIT) |
| `refactor_synthesis` | RefactoringMiner (originally planned, dropped in v0.8) | — (recipe is original) | [tsantalis/RefactoringMiner](https://github.com/tsantalis/RefactoringMiner) (MIT) |

### Shared components (not pipelines)

| Component | Inspiration | Link |
|---|---|---|
| `reward.py` (diff-similarity) | SWE-RL | [arXiv:2502.18449](https://arxiv.org/abs/2502.18449) · [facebookresearch/swe-rl](https://github.com/facebookresearch/swe-rl) — concept only; their reward lib is CC BY-NC 4.0, our reimpl is Apache-2.0 |
| `bootstrap/` (LLM Docker env gen) | RepoLaunch | [microsoft/RepoLaunch](https://github.com/microsoft/RepoLaunch) (MIT) · [arXiv:2603.05026](https://arxiv.org/abs/2603.05026) |
| `_oss_instruct.py` | Magicoder | [ise-uiuc/magicoder](https://github.com/ise-uiuc/magicoder) (MIT) |
| `_function_extractor.py` | R2E (`fut_extractor`) | [r2e-project/r2e](https://github.com/r2e-project/r2e) (MIT) |
| `_mutation_operators.py` | SWE-smith | [SWE-bench/SWE-smith](https://github.com/SWE-bench/SWE-smith) (MIT) |

## Related work & other implementations

Adjacent work not directly cited above, grouped by the technique it overlaps.
Useful if you're deciding whether to extend a pipeline, swap a reward, or feed
our datasets into a trainer.

### RL-environment frameworks & training stacks (the "consumption" layer)

- **NeMo Gym** (NVIDIA) — [NVIDIA-NeMo/Gym](https://github.com/NVIDIA-NeMo/Gym) (Apache-2.0). 100+ RLVR environments for LLMs incl. Mini-SWE-Agent / OpenHands SWE agents and a Harbor harness; the closest analogue to Repo2RLEnv's goal, sharing the Harbor ecosystem.
- **SkyRL** (UC Berkeley NovaSky) — [NovaSky-AI/SkyRL](https://github.com/NovaSky-AI/SkyRL) (Apache-2.0) · *SkyRL-Agent* [arXiv:2511.16108](https://arxiv.org/abs/2511.16108). Full-stack RL training framework targeting long-horizon SWE-bench-style agentic tasks; references Harbor.
- **verifiers** (Prime Intellect) — [PrimeIntellect-ai/verifiers](https://github.com/PrimeIntellect-ai/verifiers). Defines "environment = dataset + harness + rubric/reward", the closest competing spec to our task+verifier emission.
- **OpenEnv** (Meta PyTorch + Hugging Face) — [meta-pytorch/OpenEnv](https://github.com/meta-pytorch/OpenEnv). Gymnasium-style `step()/reset()/state()` standard for containerized agentic environments with a shared Hub; adjacent standardization effort to our Harbor emission + HF Hub bridge.
- **rLLM** (Agentica) — [rllm-org/rllm](https://github.com/rllm-org/rllm). "Run agent → collect traces → reward → update" post-training stack; used to train DeepSWE on R2E-Gym environments.

### Automated environment setup / dockerization (the `bootstrap/` problem)

- **RepoLaunch** (Microsoft) — [arXiv:2603.05026](https://arxiv.org/abs/2603.05026) · [microsoft/SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live) (MIT). LLM agent for polyglot, multi-OS automated build/test env setup — the canonical citation for our bootstrap layer.
- **Repo2Run** — [arXiv:2502.13681](https://arxiv.org/abs/2502.13681). Automated building of executable environments for code repositories at scale; direct overlap with `ensure_bootstrap`.
- **EnvBench** (JetBrains Research, ICLR '25 DL4Code) — [arXiv:2503.14443](https://arxiv.org/abs/2503.14443). 994-repo benchmark for automated env configuration with compilation/import success checks.
- **SetupBench** (Microsoft) — [arXiv:2507.09063](https://arxiv.org/abs/2507.09063) · [microsoft/SetupBench](https://github.com/microsoft/SetupBench). 93 tasks isolating the "bootstrap a dev env from a bare OS" skill across 7 ecosystems.

### SWE training datasets & RL recipes

- **SWE-Gym** (Pan et al., ICML '25) — [arXiv:2412.21139](https://arxiv.org/abs/2412.21139) · [SWE-Gym/SWE-Gym](https://github.com/SWE-Gym/SWE-Gym). 2,438 real Python tasks with executable runtimes + tests for *training* agents/verifiers.
- **SWE-rebench** (Nebius) — [arXiv:2505.20411](https://arxiv.org/abs/2505.20411) · [nebius/SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench). Continuous automated extraction of 21k+ interactive Python tasks from live repos with contamination tracking — the closest large-scale mining-to-RL pipeline to ours.
- **Agent-RLVR** — [arXiv:2506.11425](https://arxiv.org/abs/2506.11425). Training SWE agents via guidance + environment rewards; relevant to our verifier-graded reward design.
- **DeepSWE** (Agentica + Together) — [agentica-org/DeepSWE-Preview](https://huggingface.co/agentica-org/DeepSWE-Preview). RL-only coding agent (Qwen3-32B) trained on ~4,500 R2E-Gym tasks; a reference end-to-end consumer of mined repo environments.
- **Kimi-Dev** (Moonshot AI) — [arXiv:2509.23045](https://arxiv.org/abs/2509.23045). Agentless skill-prior training + outcome-driven RL on issue resolution.

### Synthetic task / test synthesis

- **SWE-Flow** (Alibaba Qwen, ICML '25) — [arXiv:2506.09003](https://arxiv.org/abs/2506.09003) · [Hambaobao/SWE-Flow](https://github.com/Hambaobao/SWE-Flow). Builds a Runtime Dependency Graph from unit tests to synthesize verifiable TDD tasks; overlaps `equivalence_tests` and test-anchored synthesis.
- **SWE-Mirror** — [arXiv:2509.08724](https://arxiv.org/abs/2509.08724). Mirrors real issues across other repos to produce 60K verifiable tasks in 4 languages, kept only if test-status transitions validate.
- **FEA-Bench** (PKU + MSR Asia, ACL '25) — [arXiv:2503.06680](https://arxiv.org/abs/2503.06680) · [microsoft/FEA-Bench](https://github.com/microsoft/FEA-Bench). PR-mined, intent-filtered feature-implementation tasks with unit tests — overlaps our PR-mining + structural filters + test verifier.
- **OpenCodeInstruct** (NVIDIA) — [arXiv:2504.04030](https://arxiv.org/abs/2504.04030) · [nvidia/OpenCodeInstruct](https://huggingface.co/datasets/nvidia/OpenCodeInstruct). 5M instruction samples with test cases + execution feedback — close in spirit to `code_instruct`.
- **Genetic-Instruct** (NVIDIA) — [arXiv:2407.21077](https://arxiv.org/abs/2407.21077). Evolutionary (mutation/crossover) scaling of synthetic coding instructions; comparable to `mutation_bugs`' procedural generation.

### Model lines trained with verifiable code RL (downstream consumers)

These are where datasets like ours ultimately go — model families whose post-training includes RL with verifiable code rewards.

- **NVIDIA Nemotron** — *Nemotron-Cascade* [arXiv:2512.13607](https://arxiv.org/abs/2512.13607), *PivotRL* [arXiv:2603.21383](https://arxiv.org/abs/2603.21383); SWE RL datasets [nvidia/Nemotron-Cascade-RL-SWE](https://huggingface.co/datasets/nvidia/Nemotron-Cascade-RL-SWE) and [nvidia/Nemotron-RL-Agentic-SWE-Pivot-v1](https://huggingface.co/datasets/nvidia/Nemotron-RL-Agentic-SWE-Pivot-v1). Multi-environment RLVR via NeMo Gym for agentic coding. Note the contrast: Nemotron-Cascade uses an *execution-free* reward model, vs. our test-pass-rate execution verification.
- **NVIDIA AceReason-Nemotron** — [arXiv:2505.16400](https://arxiv.org/abs/2505.16400), [v1.1 arXiv:2506.13284](https://arxiv.org/abs/2506.13284) · [nvidia/AceReason-Nemotron-14B](https://huggingface.co/nvidia/AceReason-Nemotron-14B). Large-scale GRPO RL with test-case-backed code prompts — a core RLVR-on-code reference.
- **NVIDIA OpenCodeReasoning** (COLM '25) — [arXiv:2504.01943](https://arxiv.org/abs/2504.01943) · [nvidia/OpenCodeReasoning](https://huggingface.co/datasets/nvidia/OpenCodeReasoning). SFT-only reasoning dataset matching RL-trained models on LiveCodeBench — a distillation-vs-RL counterpoint.
- **NVIDIA Llama-Nemotron** — [arXiv:2505.00949](https://arxiv.org/abs/2505.00949) · [NVIDIA/NeMo-Skills](https://github.com/NVIDIA/NeMo-Skills) (Apache-2.0, the synthesis+training infra behind these). Post-training with large-scale verifiable-reward RL.
- **Microsoft Phi-4-reasoning** — [arXiv:2504.21318](https://arxiv.org/abs/2504.21318) (and *Phi-4-Mini-Reasoning* [arXiv:2504.21233](https://arxiv.org/abs/2504.21233)). Outcome-based RL with verifiable rewards; coding among eval domains.
- **Microsoft rStar-Math** — [arXiv:2501.04519](https://arxiv.org/abs/2501.04519) · [microsoft/rStar](https://github.com/microsoft/rStar) (MIT). Execution-verified, code-augmented reasoning with a process reward model.

---

*Last refreshed: 2026-06. Spotted something missing or mis-cited? PRs to this page welcome.*
