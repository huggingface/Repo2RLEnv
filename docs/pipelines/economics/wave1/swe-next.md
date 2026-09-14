# swe-next: Wave 1 measured campaign

Snapshot: 2026-09-14T15:38:31.457136+00:00. All generation targets reached.

## Inventory and economics

- Retained exports: 20; new exports: 80; total: 100/100.
- New model usage booked: $7.5144; outstanding model holds: $0.0000.
- Cloud usage is reconciled separately; these model costs are not an all-in task price.
- Exported, execution-checked and quality-accepted counts are distinct.

## Source diversity

| Repository or reasoning family | Exports, including retained tasks |
|---|---:|
| https://github.com/Suor/funcy | 9 |
| https://github.com/andialbrecht/sqlparse | 8 |
| https://github.com/dbader/schedule | 8 |
| https://github.com/mahmoud/boltons | 25 |
| https://github.com/more-itertools/more-itertools | 20 |
| https://github.com/pallets/itsdangerous | 6 |
| https://github.com/pypa/packaging | 24 |

## Runs

| Run | State | Exports | Booked model USD | Elapsed minutes |
|---|---|---:|---:|---:|
| swe-next-boltons-daytona-01 | completed | 25 | 0.8182 | 34.1 |
| swe-next-cachetools-expand02 | completed | 0 | 0.0000 | 0.5 |
| swe-next-funcy-daytona-01 | completed | 9 | 0.2637 | 12.7 |
| swe-next-itsdangerous-pilot | completed | 6 | 0.2653 | 12.6 |
| swe-next-packaging-daytona-01 | completed | 24 | 5.5060 | 33.8 |
| swe-next-python-sortedcontainers-expand02 | completed | 0 | 0.0000 | 0.2 |
| swe-next-schedule-dependencies02 | completed | 8 | 0.2469 | 9.5 |
| swe-next-schedule-expand02 | completed | 0 | 0.0000 | 3.4 |
| swe-next-sqlparse-expand02 | completed | 8 | 0.2380 | 10.5 |
| swe-next-toolz-daytona-01 | completed | 0 | 0.1763 | 11.2 |

See the [campaign protocol](../../wave1_scale100.md) for validation levels,
cost attribution, known limitations and the documentation contract. Full local
receipts are under `workspace/owned-wave1-100/`; generated source and raw model
traces remain outside Git and outside learner-visible verification assets.
