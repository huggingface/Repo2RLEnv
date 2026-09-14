# Six-recipe expansion economics

Snapshot: 2026-09-14T17:04:52.199555+00:00

Generation exports, not independent quality acceptance. Parent reservations and child costs describe the same money. Active cloud usage remains reserved until reconciliation; a zero booked compute amount is not free compute.

| Recipe | Retained | New exports | Total / 100 | Model USD | Other accounted USD | Reserved USD | Available USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| swe-flow | 24 | 59 | 83 | 3.186973 | 14.577607 | 4.350000 | 77.885420 |
| seta-seed2synth | 23 | 25 | 48 | 11.933175 | 1.213308 | 12.200000 | 74.653517 |
| seta-evol | 20 | 34 | 54 | 12.913753 | 4.534414 | 13.100000 | 69.451833 |
| tmax | 20 | 7 | 27 | 15.82515 | 0 | 12.750000 | 71.424850 |
| terminalworld | 20 | 26 | 46 | 17.273195 | 4.41425 | 16.900000 | 61.412555 |
| dataarc | 20 | 56 | 76 | 9.772728 | 7.667744 | 8.650000 | 73.909528 |

Each recipe has a $100 cap. Model charges include recorded unsuccessful attempts;
other accounted amounts include estimated worker/build costs once reconciled.
Retained task generation costs, interactive assistant usage and future independent
quality review are outside these new-generation figures. A bounded parallel queue
fills the remaining target with fixed source selections and separate recipe caps.

See the [campaign guide](../../waves34_scale100.md) for inputs, prompt changes and
execution boundaries. Detailed local ledgers, model receipts, events and task paths
are recorded in `workspace/owned-waves34-100/campaigns/<recipe>/`. The local
`reports/progress.json` also records per-run failures and output lineage.
