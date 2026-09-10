# Recorded deviations

`001-resource-limits.patch` adds SDK per-session dollar/turn caps and flushes existing
logs so a remote controller can observe progress. It leaves model IDs, prompts, tools,
hooks and native acceptance logic unchanged. Smoke settings: $8 and 100 turns per
author session, with a 45-minute outer command timeout. SDK limits can overshoot by
an in-flight response; the campaign reserves additional allowance.

The standalone synthesis installation selects its actual dependencies rather than
installing the unrelated GPU/training stack from the repository-wide environment
dump. The resolved remote environment is saved as an installation receipt.

The published seed split returns 403 for the configured HF account. `prepare_seeds.py`
independently fetches five public Unix.SE questions and accepted answers, preserving
the original adapter's documented schema, URLs and attribution. This is a new input
sample of the same native source type, not a reproduction of the gated split.
