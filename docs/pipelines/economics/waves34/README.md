# Six-recipe expansion economics

Snapshot: 2026-09-14T16:38:43.933381+00:00

Generation exports, not independent quality acceptance. Parent reservations and child costs describe the same money. Active cloud usage remains reserved until reconciliation; a zero booked compute amount is not free compute.

| Recipe | Retained | New exports | Total / 100 | Model USD | Other accounted USD | Reserved USD | Available USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| swe-flow | 24 | 23 | 47 | 0.9655 | 4.126736 | 6.900000 | 88.007764 |
| seta-seed2synth | 23 | 9 | 32 | 4.443906 | 1.213308 | 8.650000 | 85.692786 |
| seta-evol | 20 | 10 | 30 | 4.311869 | 2.16835 | 10.050000 | 83.469781 |
| tmax | 20 | 3 | 23 | 5.380179 | 0 | 8.150000 | 86.469821 |
| terminalworld | 20 | 11 | 31 | 4.376309 | 1.071365 | 7.900000 | 86.652326 |
| dataarc | 20 | 20 | 40 | 4.095325 | 1.105439 | 6.150000 | 88.649236 |

Each recipe has a $100 cap. Model charges include recorded unsuccessful attempts;
other accounted amounts include estimated worker/build costs once reconciled.
Retained task generation costs, interactive assistant usage and future independent
quality review are outside these new-generation figures. A bounded parallel queue
fills the remaining target with fixed source selections and separate recipe caps.

See the [campaign guide](../../waves34_scale100.md) for inputs, prompt changes and
execution boundaries. Detailed local ledgers, model receipts, events and task paths
are recorded in `workspace/owned-waves34-100/campaigns/<recipe>/`. The local
`reports/progress.json` also records per-run failures and output lineage.
