# September 10 reproduction pilot costs

The user authorized a **new $500 budget**, separate from earlier Tasksmith
campaigns. This pilot accounts for **$15.45812590**, leaving **$484.54187410**.
No paid jobs remain running and no operation reservations remain open.

| Category | Accounted USD | Evidence type |
| --- | ---: | --- |
| SETA Seed2Synth, three native tasks | 7.28813200 | Claude SDK reported |
| SETA Evol, two native children | 2.86405710 | Claude SDK reported |
| Endless generation, conversion and solver attempts | 0.50125250 | Response usage × installed LiteLLM rate table |
| TMax generation and solver attempts | 2.10835500 | Response usage × installed LiteLLM rate table |
| SWE-smith original issue writer | 0.01877200 | Response usage estimate |
| Two blind Harbor Sonnet rollouts | 0.11237430 | Harbor agent cost estimates |
| Shared Modal worker, including build allowance | 2.56518300 | Conservative cloud estimate |
| **Total** | **15.45812590** | **Not a provider invoice** |

The cloud estimate covers 5,919.755226 seconds from worker creation to confirmed
termination, using the full requested four cores and 16 GiB throughout. It uses
the published Sandbox rates of $0.00003942/core/second and
$0.00000667/GiB/second, then adds $1 for image-building uncertainty.
[Modal pricing](https://modal.com/pricing), checked September 10, 2026.

Actual metering, credits and ancillary fees may differ. Model cost reports also
are not invoice reconciliation. Raw response usage, SDK ResultMessages, all
operation settlements, the explicit cloud calculation and the worker teardown
receipt are retained in the [artifact repository](ARTIFACTS.md).

Failed operations are included. Probes that failed before any model request are
recorded as not started at $0; an interrupted TMax solver retains its 21 successful
API response receipts. The failed timing parent was excluded before evolution,
so its unused allowance was released after the selected child's usage settled.
