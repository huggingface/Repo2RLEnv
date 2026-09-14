# Yield and cost per task

Observed samples measured on **2026-09-14**. Use these as measured examples, not price guarantees. A task is one exported Harbor environment; an export is not independent quality acceptance.

## Generation

Costs include unsuccessful attempts and bounded repairs within each sample. Model and estimated compute costs are separate; the total is shown only when both are attributable. **— means unavailable, not zero.**

| Pipeline | Attempted candidates | New tasks | Yield | Model / task | Compute / task | Total generation / task |
|---|---:|---:|---:|---:|---:|---:|
| [swe-smith](repo_mutate.md) | — | 76 | — | $0.03 | — | — |
| [r2e](r2e.md) | — | 80 | — | $0.22 | — | — |
| [swe-gen](pr_to_env.md) | — | 80 | — | $0.02 | — | — |
| [swe-next](swe_next.md) | — | 80 | — | $0.09 | — | — |
| [r2e-gym](r2e_gym.md) | — | 80 | — | $0.04 | — | — |
| [scaler](scaler.md) | — | 80 | — | $0.00 | — | — |
| [endless-terminals](endless_terminals.md) | 99 | 80 | 80.8% | $0.37 | $0.15 | $0.52 |
| [cli-gym](env_repair.md) | — | 5 | — | $0.71 | $0.43 | $1.14 |
| [swe-flow](repo_reconstruct.md) | — | 76 | — | $0.05 | $0.25 | $0.30 |
| [seta-seed2synth](terminal_synth.md) | 112 | 77 | 68.8% | $0.54 | $0.17 | $0.71 |
| [seta-evol](task_evolve.md) | 92 | 80 | 87.0% | $0.38 | $0.17 | $0.56 |
| [tmax](tmax.md) | 104 | 35 | 33.7% | $1.68 | $0.42 | $2.10 |
| [terminalworld](terminalworld.md) | 1293 | 80 | 6.2% | $0.68 | $0.58 | $1.26 |
| [dataarc](dataarc.md) | 83 | 80 | 96.4% | $0.18 | $0.15 | $0.33 |
| [tasksmith](tasksmith.md) | — | 26 | — | — | — | — |

## How to read the measurements

**Yield = new exports / distinct recorded task candidates.** Count a candidate once across retries. The available terminal-generator counts start before design screening; filtered and interrupted candidates remain in the denominator. Failures before a candidate identity exists are not counted as candidates, although their costs are included. Older repository runs do not retain a consistently deduplicated denominator, so their yield is unavailable. Reaching a dataset target is not a yield measurement.

TerminalWorld's yield includes recordings rejected as unsuitable; it is not the success rate among already approved designs. SETA Evol includes 1 unfinished candidate and TMax 8 when generation stopped. These input domains are not a controlled ranking of algorithms.

SWE-smith, R2E, SWE-gen, SWE-Next, R2E-Gym and SCALER shared workers. Together their 476 new exports cost **$75.90, or $0.159 per task**, including estimated compute. Per-recipe compute was not allocated. SCALER uses no generation-model calls, but still incurs compute cost.

The CLI-Gym cost sample has 5 new tasks; its published dataset contains 25. Earlier retained-task costs and interactive assistant usage are excluded throughout. Most recipe samples used CPU workers on Daytona; Tasksmith also used native Modal GPU execution. These measurements include development retries and do not establish unattended production cost.

The primary recipe author was `claude-sonnet-4-6`; TMax also includes a small unsuccessful `gpt-5.4-mini` comparison. SCALER used no generation model. Configured model identities are retained in the summary; these are not cross-model quality comparisons.

Uncertain model charges remain excluded from recorded totals: $2.25 for recipes sharing workers, $1.25 for SETA Evol and $5.00 for TMax. They may increase the final cost. Compute figures are resource estimates, not provider invoices.

## Tasksmith and optional evaluation

Tasksmith's measured expansion added **26 accepted PR tasks** and also repaired/revalidated retained tasks. Its **$15.08 per added accepted task** includes authoring, quality review, rollouts and compute. It is not a generation-only price and is excluded from the generation-cost columns above.

| Component | Recorded cost per added accepted task |
|---|---:|
| Authoring model | $2.46 |
| Review and repair models | $3.31 |
| Blind solver model | $0.82 |
| Compute across generation and evaluation | $8.49 |
| Total | $15.08 |

A further $4.98 remains unresolved for this sample. The final published Tasksmith cohort has 50 verified tasks, including 19 full Sonnet solves. Solver success, generation yield and quality acceptance are separate measures. Comparable independent evaluation costs have not been established for the other full datasets.

## Measurement source

The [sanitized summary](../data/pipelines.json) contains sample sizes, cost scopes, candidate-count definitions and pinned dataset manifests. No private campaign folder is needed to rebuild this page. To update both tables after reviewing new measurements:

```bash
python docs/_tools/generate_metrics.py
python docs/_tools/generate_metrics.py --check
```
