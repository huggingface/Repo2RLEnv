# `pr_runtime / swe_next`

SWE-Next turns real historical code changes into repository repair tasks.

```text
Merged PR metadata → merge commit and first parent → bounded Python/test changes → original-path post-change tests
  → run identical tests against old and new code
  → require failing-to-passing behavior and no regression
  → author a natural issue from the observed failure
  → scrubbed old repository + private tests + reference
  → fresh Harbor baseline/reference execution → export
```

Run `repo2rlenv generate --config examples/owned-swe-next.yaml`.
The first profile supports public GitHub Python repositories and ordinary pytest
files. Source paths, dependency installation and test roots are explicit. All
repository execution and image builds run on the configured Modal or Daytona worker.

The default original-path test layout is retained. Quarterly LLM environment profiles are replaced by an explicit dependency profile and the existing content-addressed bootstrap cache. The native intersection/file-level comparison fallback is deliberately replaced by exact nonempty test identity equality for this generation profile.

`target`, `max_candidates`, `history_limit` and change-size bounds control the run.
`max_prs` bounds API discovery; optional `pr_numbers` selects explicit merged PRs.
The first target is **20 generated tasks**. The runtime records source exclusions,
bootstrap failures and execution results. A reference success is a generation
check; detailed quality acceptance follows the full campaign.

The export carries the old repository context, private post-change tests, a
reference repair and deterministic test reward. Changed source files must already
exist; added/deleted implementation files and specialized test runners need a
separate supported profile. Dependencies are available before offline solving.

Credit: [SWE-Next](https://github.com/TIGER-AI-Lab/SWE-Next), Apache-2.0,
commit `b55c0841f364f9fe7363b2012cd0ae8d8afdf872`. See
[RFC 0023](../rfcs/0023-swe-next-recipe.md), the packaged `recipes/swe_next/provenance.md`,
and the [shared CLI and cloud guide](owned_recipes.md).
