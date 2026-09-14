# swe-smith: Wave 1 measured campaign

Snapshot: 2026-09-14T15:38:31.457136+00:00. All generation targets reached.

## Inventory and economics

- Retained exports: 24; new exports: 76; total: 100/100.
- New model usage booked: $2.4075; outstanding model holds: $0.0000.
- Cloud usage is reconciled separately; these model costs are not an all-in task price.
- Exported, execution-checked and quality-accepted counts are distinct.

## Source diversity

| Repository or reasoning family | Exports, including retained tasks |
|---|---:|
| https://github.com/Suor/funcy | 23 |
| https://github.com/mahmoud/boltons | 19 |
| https://github.com/more-itertools/more-itertools | 24 |
| https://github.com/pallets/itsdangerous | 3 |
| https://github.com/pypa/packaging | 15 |
| https://github.com/pytoolz/toolz | 16 |

## Runs

| Run | State | Exports | Booked model USD | Elapsed minutes |
|---|---|---:|---:|---:|
| smith-boltons-pilot | completed | 8 | 0.2031 | 4.3 |
| smith-funcy-expand | cancelled | 7 | 0.1565 | 14.6 |
| swe-smith-boltons-daytona-01 | completed | 11 | 0.2414 | 17.1 |
| swe-smith-funcy-daytona-01 | completed | 16 | 0.3153 | 17.4 |
| swe-smith-itsdangerous-daytona-01 | completed | 3 | 0.1562 | 3.9 |
| swe-smith-packaging-daytona-01 | failed | 0 | 0.0000 | 27.6 |
| swe-smith-packaging-recovery-02 | completed | 15 | 0.4316 | 35.1 |
| swe-smith-toolz-daytona-01 | failed | 0 | 0.0000 | 0.3 |
| swe-smith-toolz-daytona-repaired | completed | 0 | 0.8379 | 23.9 |
| swe-smith-toolz-recovery-02 | completed | 16 | 0.0656 | 14.7 |

See the [campaign protocol](../../wave1_scale100.md) for validation levels,
cost attribution, known limitations and the documentation contract. Full local
receipts are under `workspace/owned-wave1-100/`; generated source and raw model
traces remain outside Git and outside learner-visible verification assets.
