# Six-recipe expansion economics

Snapshot: 2026-09-14T18:23:30.592522+00:00

Generation exports, not independent quality acceptance. Parent reservations and child costs describe the same money. Active cloud usage remains reserved until reconciliation; a zero booked compute amount is not free compute.

| Recipe | Retained | New exports | Total / 100 | Model USD | Other accounted USD | Reserved USD | Available USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| swe-flow | 24 | 76 | 100 | 4.049313 | 18.857685 | 0.000000 | 77.093002 |
| seta-seed2synth | 23 | 69 | 92 | 37.37191 | 10.229778 | 6.100000 | 46.298312 |
| seta-evol | 20 | 80 | 100 | 30.750897 | 13.969454 | 1.250000 | 54.029649 |
| tmax | 20 | 32 | 52 | 51.003181 | 4.641647 | 16.950000 | 27.405172 |
| terminalworld | 20 | 73 | 93 | 52.307037 | 36.856839 | 5.400000 | 5.436124 |
| dataarc | 20 | 80 | 100 | 14.530915 | 12.03637 | 0.000000 | 73.432715 |

Each recipe has a $100 cap. Model charges include recorded unsuccessful attempts;
other accounted amounts include estimated worker/build costs once reconciled.
Retained task generation costs, interactive assistant usage and future independent
quality review are outside these new-generation figures. A bounded parallel queue
fills the remaining target with fixed source selections and separate recipe caps.

See the [campaign guide](../../waves34_scale100.md) for inputs, prompt changes and
execution boundaries. Detailed local ledgers, model receipts, events and task paths
are recorded in `workspace/owned-waves34-100/campaigns/<recipe>/`. The local
`reports/progress.json` also records per-run failures and output lineage.
