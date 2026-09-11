# `commit_runtime / r2e_gym`

R2E-Gym / SWEGEN turns real historical code changes into repository repair tasks.

```text
First-parent commit history → bounded bug edits and matching test changes → extracted post-change test files under r2e_tests
  → run identical tests against old and new code
  → require failing-to-passing behavior and no regression
  → author a natural issue from the observed failure
  → scrubbed old repository + private tests + reference
  → fresh Harbor baseline/reference execution → export
```

Run `repo2rlenv generate --config examples/owned-r2e-gym.yaml`.
The first profile supports public GitHub Python repositories and ordinary pytest
files. Source paths, dependency installation and test roots are explicit. All
repository execution and image builds run on the configured Modal or Daytona worker.

The native strict test identity comparison is retained. Test files are renamed under r2e_tests. Repository-specific Pillow, NumPy, Datalad and Tornado import/runner heuristics are not supported in the initial ordinary-pytest profile. The native optional bug-edit/test-match switches default on, as in the published generation guide.

`target`, `max_candidates`, `history_limit` and change-size bounds control the run.
`require_bug_edit` and `require_test_match` expose the native filter switches.
The first target is **20 generated tasks**. The runtime records source exclusions,
bootstrap failures and execution results. A reference success is a generation
check; detailed quality acceptance follows the full campaign.

The export carries the old repository context, private post-change tests, a
reference repair and deterministic test reward. Changed source files must already
exist; added/deleted implementation files and specialized test runners need a
separate supported profile. Dependencies are available before offline solving.

Credit: [R2E-Gym / SWEGEN](https://github.com/R2E-Gym/R2E-Gym), Apache-2.0,
commit `0d94c4eb9431cd195c55a7ea3abd54006c9a1735`. See
[RFC 0024](../rfcs/0024-r2e-gym-recipe.md), the packaged `recipes/r2e_gym/provenance.md`,
and the [shared CLI and cloud guide](owned_recipes.md).
