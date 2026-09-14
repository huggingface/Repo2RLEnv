# r2e: Wave 1 measured campaign

Snapshot: 2026-09-14T15:38:31.457136+00:00. All generation targets reached.

## Inventory and economics

- Retained exports: 20; new exports: 80; total: 100/100.
- New model usage booked: $17.7254; outstanding model holds: $1.5000.
- Cloud usage is reconciled separately; these model costs are not an all-in task price.
- Exported, execution-checked and quality-accepted counts are distinct.

## Source diversity

| Repository or reasoning family | Exports, including retained tasks |
|---|---:|
| https://github.com/Suor/funcy | 16 |
| https://github.com/andialbrecht/sqlparse | 11 |
| https://github.com/dgilland/pydash | 8 |
| https://github.com/grantjenks/python-sortedcontainers | 1 |
| https://github.com/mahmoud/boltons | 25 |
| https://github.com/more-itertools/more-itertools | 20 |
| https://github.com/pallets/itsdangerous | 3 |
| https://github.com/pypa/packaging | 7 |
| https://github.com/tkem/cachetools | 9 |

## Runs

| Run | State | Exports | Booked model USD | Elapsed minutes |
|---|---|---:|---:|---:|
| r2e-boltons-daytona-01 | completed | 25 | 4.2050 | 68.6 |
| r2e-cachetools-expand02 | completed | 9 | 1.4169 | 25.0 |
| r2e-funcy-daytona-01 | failed | 7 | 0.8915 | 15.6 |
| r2e-funcy-pilot | cancelled | 9 | 0.5422 | 21.7 |
| r2e-itsdangerous-daytona-01 | completed | 3 | 0.1381 | 4.5 |
| r2e-packaging-daytona-01 | failed | 7 | 3.5105 | 78.2 |
| r2e-pydash-expand03 | completed | 3 | 0.1833 | 5.8 |
| r2e-pydash-refill04 | completed | 2 | 0.0907 | 3.5 |
| r2e-pydash-refill05 | completed | 3 | 0.1405 | 5.3 |
| r2e-python-sortedcontainers-expand02 | completed | 1 | 0.0302 | 1.7 |
| r2e-sqlparse-expand02 | completed | 11 | 1.5979 | 31.2 |
| r2e-toolz-daytona-01 | cancelled | 0 | 4.9788 | 53.7 |

See the [campaign protocol](../../wave1_scale100.md) for validation levels,
cost attribution, known limitations and the documentation contract. Full local
receipts are under `workspace/owned-wave1-100/`; generated source and raw model
traces remain outside Git and outside learner-visible verification assets.
