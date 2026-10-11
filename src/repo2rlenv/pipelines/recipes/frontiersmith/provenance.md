# FrontierSmith method attribution

Inspired by **FrontierSmith: Synthesizing Open-Ended Coding Problems at Scale**,
Runyuan He, Qiuyang Mang and collaborators, 2026.

- Paper: https://arxiv.org/abs/2605.14445
- Repository inspected: https://github.com/FrontierCS/FrontierSmith
- Revision inspected: `166c8be14a62b013ef78e6868e4355421c8c6456`

The upstream release withholds the synthesis orchestrator and LLM-driven generator
and checker authors. No upstream code, prompts, test cases, statements or adapter
are bundled here. This recipe, its prompts and the example seed descriptions are
original Repo2RLEnv code under Apache-2.0. We do not assert a license for upstream
assets. The scientific method is credited; training results are not reproduced.

Preserved ideas: formulation mutation along three axes, independent solution
sampling, semantic and execution diversity, separately authored test generators
and verifiers, deterministic continuous rewards and bounded cross-validation.

Pilot differences: original seeds instead of HardTests, Python instead of C++,
OpenAI-only generation, three solver samples by default, threshold filtering
instead of population top-N ranking, no recursive seed evolution or RL training.
