# Wave 1 generation economics

Final generation snapshot: 14 September 2026, 15:22 UTC. All six recipes have
100 local Harbor exports. The campaign added 476 tasks to 124 retained exports.
These are generation counts; independent quality acceptance remains separate.

| Recipe | New exports | Recorded model cost | Model cost per new export | Detailed report |
|---|---:|---:|---:|---|
| SWE-smith | 76 | $2.407499 | $0.0317 | [Sources, attempts and timing](swe-smith.md) |
| R2E | 80 | $17.725424 | $0.2216 | [Sources, attempts and timing](r2e.md) |
| SWE-gen | 80 | $1.634643 | $0.0204 | [Sources, attempts and timing](swe-gen.md) |
| SWE-next | 80 | $7.514418 | $0.0939 | [Sources, attempts and timing](swe-next.md) |
| R2E-Gym | 80 | $3.523542 | $0.0440 | [Sources, attempts and timing](r2e-gym.md) |
| SCALER | 80 | $0 | $0 | [Families, attempts and timing](scaler.md) |

Recorded model usage totals **$32.805526**, including failed attempts and repairs.
Shared cloud/build estimates add **$43.093688**, giving **$75.899214 accounted**.
The campaign retains **$2.25 of uncertain model reservations**. Its $150 ledger
therefore has $71.850786 available. Cloud estimates include the recorded
conservative build allowances; they are not provider invoices. The shared workers
served multiple recipes, so this table does not present model-only costs as total
task prices or allocate shared costs to recipes without supporting measurements.

Across the 476 new exports, accounted model plus estimated cloud/build spend is
approximately **$0.159 per export**. This excludes the earlier generation cost of
the 124 retained tasks, interactive assistant usage and future independent review
or blind solver rollouts. It is an observed campaign average, not a forecast.

The local ledger and `workspace/owned-wave1-100/reports/progress.json` preserve
the itemized evidence. The [completion report](../../wave1_scale100.md) explains
execution-control coverage, recovery fixes and the limits of these results.

At 15:36 UTC, $40 of the available balance was reserved for the authorized final
Endless Terminals expansion. This is a Wave 2 child allocation and is excluded
from all Wave 1 costs above. The parent ledger has $31.850786 unallocated at
dispatch; its combined limit is unchanged. Live reports separate child
allocations from generation costs in `cost_attribution`.
