# Six-recipe expansion economics

Snapshot: 2026-09-14T17:46:18.393297+00:00

Generation exports, not independent quality acceptance. Parent reservations and child costs describe the same money. Active cloud usage remains reserved until reconciliation; a zero booked compute amount is not free compute.

| Recipe | Retained | New exports | Total / 100 | Model USD | Other accounted USD | Reserved USD | Available USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| swe-flow | 24 | 76 | 100 | 4.049313 | 18.857685 | 0.000000 | 77.093002 |
| seta-seed2synth | 23 | 50 | 73 | 25.691985 | 6.340815 | 11.200000 | 56.767200 |
| seta-evol | 20 | 70 | 90 | 27.537775 | 10.531992 | 8.450000 | 53.480233 |
| tmax | 20 | 23 | 43 | 34.559487 | 1.547941 | 13.000000 | 50.892572 |
| terminalworld | 20 | 57 | 77 | 40.066211 | 24.938862 | 16.700000 | 18.294927 |
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
