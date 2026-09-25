# Method credit

This independently authored implementation follows **CodeMidas: Scaling Agentic
Coding RL Environments from Code Itself**, Bowen Ye et al., Xiaomi MiMo and
collaborating institutions, [arXiv:2609.22068v1](https://arxiv.org/abs/2609.22068).

The construction prompts and research generator were not available when this
recipe was implemented. No upstream implementation, prompts, or software license
has been copied. Code in this directory is covered by Repo2RLEnv's Apache-2.0
license. The paper's arXiv distribution terms are not a software license.

The initial profile uses Python CPU repositories, GPT-6 Luna/Sol, Daytona, and
Harbor. These are explicit reproduction choices. It does not reproduce the
paper's full multi-language corpus or downstream RL training results. Four final
screening attempts are our choice; the paper does not specify that sample count.

Input repositories retain their own licenses and copyright notices. Stack v3
inputs retain the dataset revision, original repository commit, per-file content
identifiers, actual materialized hashes, and detected licenses. Inline and
GitHub-hydrated inputs have different provenance labels. Dataset inclusion is
not a replacement for the original code license.
