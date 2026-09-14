# r2e-gym: Wave 1 measured campaign

Snapshot: 2026-09-14T15:38:31.457136+00:00. All generation targets reached.

## Inventory and economics

- Retained exports: 20; new exports: 80; total: 100/100.
- New model usage booked: $3.5235; outstanding model holds: $0.7500.
- Cloud usage is reconciled separately; these model costs are not an all-in task price.
- Exported, execution-checked and quality-accepted counts are distinct.

## Source diversity

| Repository or reasoning family | Exports, including retained tasks |
|---|---:|
| https://github.com/Suor/funcy | 12 |
| https://github.com/andialbrecht/sqlparse | 8 |
| https://github.com/dbader/schedule | 6 |
| https://github.com/dgilland/pydash | 12 |
| https://github.com/mahmoud/boltons | 25 |
| https://github.com/more-itertools/more-itertools | 24 |
| https://github.com/pypa/packaging | 2 |
| https://github.com/pytoolz/toolz | 4 |
| https://github.com/tkem/cachetools | 7 |

## Runs

| Run | State | Exports | Booked model USD | Elapsed minutes |
|---|---|---:|---:|---:|
| r2e-gym-boltons-daytona-01 | completed | 25 | 0.8325 | 33.5 |
| r2e-gym-cachetools-expand02 | completed | 3 | 0.0912 | 5.9 |
| r2e-gym-cachetools-fixtures03 | completed | 4 | 0.1144 | 5.3 |
| r2e-gym-funcy-daytona-01 | completed | 12 | 0.3134 | 20.8 |
| r2e-gym-itsdangerous-daytona-01 | completed | 0 | 0.0000 | 1.2 |
| r2e-gym-more-itertools-fallback06 | completed | 4 | 0.1144 | 6.7 |
| r2e-gym-packaging-daytona-01 | failed | 2 | 1.2313 | 8.0 |
| r2e-gym-pydash-expand03 | completed | 12 | 0.3109 | 32.6 |
| r2e-gym-pydash-refill04 | completed | 0 | 0.0000 | 0.6 |
| r2e-gym-python-sortedcontainers-expand02 | completed | 0 | 0.0000 | 0.2 |
| r2e-gym-schedule-dependencies04 | completed | 6 | 0.1834 | 7.1 |
| r2e-gym-schedule-expand02 | completed | 0 | 0.0000 | 1.8 |
| r2e-gym-sqlparse-expand02 | completed | 8 | 0.2168 | 19.2 |
| r2e-gym-toolz-pilot | completed | 4 | 0.1152 | 12.3 |

See the [campaign protocol](../../wave1_scale100.md) for validation levels,
cost attribution, known limitations and the documentation contract. Full local
receipts are under `workspace/owned-wave1-100/`; generated source and raw model
traces remain outside Git and outside learner-visible verification assets.
