# Six-recipe expansion economics

Snapshot: 2026-09-14T17:33:02.529091+00:00

Generation exports, not independent quality acceptance. Parent reservations and child costs describe the same money. Active cloud usage remains reserved until reconciliation; a zero booked compute amount is not free compute.

| Recipe | Retained | New exports | Total / 100 | Model USD | Other accounted USD | Reserved USD | Available USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| swe-flow | 24 | 76 | 100 | 4.049313 | 18.857685 | 0.000000 | 77.093002 |
| seta-seed2synth | 23 | 42 | 65 | 21.422639 | 5.038774 | 10.450000 | 63.088587 |
| seta-evol | 20 | 60 | 80 | 23.090899 | 6.91442 | 12.950000 | 57.044681 |
| tmax | 20 | 18 | 38 | 28.450048 | 0 | 14.900000 | 56.649952 |
| terminalworld | 20 | 47 | 67 | 32.233409 | 16.586217 | 15.150000 | 36.030374 |
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
