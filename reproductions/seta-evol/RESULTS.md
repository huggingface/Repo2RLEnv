# SETA Evol reproduction

The original Sonnet 4.6 workflow completed **two CHANGE_CONTEXT variants from two
parents** on September 10, 2026. Both have native verdict PASS and independently
pass fresh Harbor no-op 0 / reference 1, without infrastructure exceptions.

| Parent | Child | Native runtime | SDK-reported cost |
| --- | --- | --- | --- |
| `unix-se-26047` | `unix-se-26047__b1` | 799.8 seconds | $1.41926835 |
| `unix-se-73498` | `unix-se-73498__b1` | 728.4 seconds | $1.44478875 |

Both evolve their bash debugging scenario to zsh; the second concerns shell
history configuration. Total SDK-reported cost is **$2.8640571**, not an invoice.
Exports preserve native Harbor files and the original parent identifiers.

The repeat selection also considered `unix-se-52313`, whose original reference
failed independent validation. That parent was excluded before any paid evolution;
its separately repaired variant was not silently substituted into this baseline.

This establishes repeatable native evolution, not a difficulty improvement or
curriculum result. No independent blind child rollout is counted yet.

Commands: `run_smoke.sh`, `validate_smoke.sh`, `run_repeat.sh`,
`resume_validated_repeat.sh`. Receipts and both children are in the
[artifact snapshot](../ARTIFACTS.md).
Source/runtime deviations are recorded in [patches](patches/README.md).
