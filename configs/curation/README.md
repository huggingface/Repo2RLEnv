# First campaign

`cpu-first.json` targets 30 admitted tasks with a $380 total controller cap,
including metered model calls and estimated cloud compute. The example allocates
$90 to the matched runtime comparison and $30 to earlier pilots from a $500 total.
Check existing ledgers and provider charges before starting; these limits apply
to individual output directories, not automatically across every run. Its three
concurrent candidates share one atomic budget ledger.
It uses Sonnet 5 for authorship and Sonnet 5 / Opus 5 for solver trials, with an
independent Opus 5 reviewer. These identifiers were checked against the configured
provider before the initial campaign; model availability remains account-dependent.

Use the supplied environment-specific PR Markdown file with `--seeds`, or supply
a prioritized subset. The author can frame coherent CPU-verifiable behavior from
a larger change. Hardware-dependent performance claims must not be replaced with
mock checks. Admissions retain pending human review status and one diagnostic
attempt per model, rather than claiming precise pass-rate estimates.

```bash
uv run --extra curation repo2rlenv curate \
  --seeds seeds.md --config configs/curation/cpu-first.json \
  --out workspace/curation-admission-v3
```
