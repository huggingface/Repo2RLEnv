# Six-recipe expansion economics

Snapshot: 2026-09-14T16:36:05.650827+00:00

Generation exports, not independent quality acceptance. Parent reservations and child costs describe the same money. Active cloud usage remains reserved until reconciliation; a zero booked compute amount is not free compute.

| Recipe | Retained | New exports | Total / 100 | Model USD | Other accounted USD | Reserved USD | Available USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| swe-flow | 24 | 20 | 44 | 0.881332 | 2.047713 | 6.150000 | 90.920955 |
| seta-seed2synth | 23 | 7 | 30 | 3.948228 | 1.213308 | 7.900000 | 86.938464 |
| seta-evol | 20 | 8 | 28 | 3.716167 | 2.16835 | 9.900000 | 84.215483 |
| tmax | 20 | 3 | 23 | 4.695773 | 0 | 8.800000 | 86.504227 |
| terminalworld | 20 | 8 | 28 | 3.512116 | 1.071365 | 7.800000 | 87.616519 |
| dataarc | 20 | 15 | 35 | 3.486922 | 1.105439 | 5.400000 | 90.007639 |

Each recipe has a $100 cap. Model charges include recorded unsuccessful attempts;
other accounted amounts include estimated worker/build costs once reconciled.
Retained task generation costs, interactive assistant usage and future independent
quality review are outside these new-generation figures. A bounded parallel queue
fills the remaining target with fixed source selections and separate recipe caps.

See the [campaign guide](../../waves34_scale100.md) for inputs, prompt changes and
execution boundaries. Detailed local ledgers, model receipts, events and task paths
are recorded in `workspace/owned-waves34-100/campaigns/<recipe>/`. The local
`reports/progress.json` also records per-run failures and output lineage.
