# swe-gen: Wave 1 measured campaign

Snapshot: 2026-09-14T15:38:31.457136+00:00. All generation targets reached.

## Inventory and economics

- Retained exports: 20; new exports: 80; total: 100/100.
- New model usage booked: $1.6346; outstanding model holds: $0.0000.
- Cloud usage is reconciled separately; these model costs are not an all-in task price.
- Exported, execution-checked and quality-accepted counts are distinct.

## Source diversity

| Repository or reasoning family | Exports, including retained tasks |
|---|---:|
| https://github.com/Suor/funcy | 9 |
| https://github.com/andialbrecht/sqlparse | 15 |
| https://github.com/dbader/schedule | 11 |
| https://github.com/mahmoud/boltons | 25 |
| https://github.com/more-itertools/more-itertools | 20 |
| https://github.com/pypa/packaging | 16 |
| https://github.com/tkem/cachetools | 4 |

## Runs

| Run | State | Exports | Booked model USD | Elapsed minutes |
|---|---|---:|---:|---:|
| swe-gen-boltons-daytona-01 | completed | 25 | 0.5002 | 35.3 |
| swe-gen-cachetools-expand02 | completed | 4 | 0.0819 | 9.7 |
| swe-gen-funcy-daytona-01 | completed | 9 | 0.1554 | 16.3 |
| swe-gen-itsdangerous-daytona-01 | completed | 0 | 0.0000 | 4.8 |
| swe-gen-packaging-daytona-01 | failed | 13 | 0.2487 | 61.3 |
| swe-gen-packaging-pilot | cancelled | 3 | 0.0526 | 19.4 |
| swe-gen-schedule-expand02 | completed | 11 | 0.2693 | 17.1 |
| swe-gen-sqlparse-expand02 | completed | 15 | 0.3031 | 20.7 |
| swe-gen-toolz-daytona-01 | completed | 0 | 0.0235 | 14.2 |

See the [campaign protocol](../../wave1_scale100.md) for validation levels,
cost attribution, known limitations and the documentation contract. Full local
receipts are under `workspace/owned-wave1-100/`; generated source and raw model
traces remain outside Git and outside learner-visible verification assets.
