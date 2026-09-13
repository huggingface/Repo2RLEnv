# Budget for the full HF PR list

September 13, 2026 estimate. The original list contains **114 unique PRs** across six repositories. At the 23-task checkpoint, **91 remained**. This is a planning estimate; it does not authorize additional spending.

**Plan for about $3,000 additional**, with a rounded range of **$1,800–$4,100** after contingency. The current shared campaign cap is $600. That ledger includes earlier reproduction work as well as Tasksmith, so it is not a pure per-task production cost.

| Repository | Listed | Accepted at checkpoint | Remaining | Additional generation and validation estimate |
|---|---:|---:|---:|---:|
| accelerate | 23 | 9 | 14 | $200–$450 |
| trl | 21 | 9 | 12 | $150–$350 |
| peft | 16 | 4 | 12 | $120–$300 |
| transformers | 20 | 1 | 19 | $400–$900 |
| diffusers | 20 | 0 | 20 | $400–$900 |
| tokenizers | 14 | 0 | 14 | $150–$350 |
| **Total** | **114** | **23** | **91** | **$1,420–$3,250** |

Add 25% for new image/toolchain work, retries and difficult cases: **$1,775–$4,062.50**. The ranges reflect the list’s composition and observed runs; the remaining PRs have not each received a measured cost profile.

## What the estimate includes

Each suitable PR receives a scoped instruction, offline Harbor bundle, fixed PR reference, deterministic verifier, baseline/reference comparison, wrong and valid implementation controls, one blind Sonnet rollout and trace review. Repairs are consolidated and bounded at three rounds per run. Existing accepted bundles and compatible cached images are retained. Small real models and prepared local data replace avoidable checkpoint downloads. GPU-dependent correctness is exercised on real hardware.

The first ten accepted Tasksmith environments cost about **$100.33**, including bootstrap and recovery. The later expansion accounted for about **$245.75** at thirteen additional acceptances, including work on still-unfinished candidates. Those are campaign costs, not stable marginal prices. Repeated review and verifier repairs have contributed more than the final solver calls. The corresponding evidence is in [the initial campaign](evidence/tasksmith-hf-generation.json) and [the estimate record](evidence/tasksmith-full-list-budget.json).

Current pricing for the routes used in these runs is $3/$15 per million input/output tokens for Sonnet 4.6 and $5/$25 for Opus 4.6, with separate cache rates. [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing). Modal lists L4 at approximately $0.80/GPU-hour and H100 at $3.95/GPU-hour, plus sandbox CPU and memory. [Modal pricing](https://modal.com/pricing). No model migration or discount is assumed in this estimate.

## What remains uncertain

The list includes AMD ROCm, AWS Neuron, FP8/FP4 kernels and large distributed-model changes. The current native execution profile supports one or two L4 GPUs. Fully validating additional hardware behavior requires adapter work and hardware access; these estimates are not a fixed quote for every such case. Several workflow-only or dependency-maintenance PRs may provide little training value at their original scope. Funding every attempt cannot guarantee 114 useful accepted environments.

Tokenizers also needs Rust source collection and verification support: the current Tasksmith exporter accepts Python source changes. Its successful repository bootstrap does not establish end-to-end Rust PR conversion. That adapter work must precede the tokenizers production batch.

This budget excludes human engineering labor, subsequent RL training, and broad evaluation sweeps across multiple solver models. Release funds in batches of ten, report cost per accepted task and unfinished candidate, then update the forecast from the observed mix.

The observed accepted tasks include direct engineering intervention in runtime setup,
verifier design, semantic controls and recovery. The estimate assumes that assistance
continues while recurring corrections move into the pipeline. It is not an estimate
backed by demonstrated unattended conversion of all 114 PRs. Assistant-session usage
outside the campaign ledger is also excluded.
