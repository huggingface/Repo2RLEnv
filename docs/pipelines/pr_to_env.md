# `pr_to_env / swe_gen`

The owned SWE-gen recipe turns explicit public GitHub PR URLs into standalone
Harbor tasks. It retains the upstream substantiality/instruction prompt and
healthy-head reverse-patch workflow, with an explicit Python environment profile.

```text
PR URLs → pinned metadata and source diff → cached healthy head
       → reverse source changes; retain head tests → fresh test contrast
       → substantiality + issue instruction → Harbor baseline/reference → export
```

Run `repo2rlenv generate --config examples/owned-swe-gen.yaml` after creating a
campaign ledger, a Modal or Daytona worker receipt, and a wheel of this checkout.
These use the same [owned recipe commands](owned_recipes.md) as SWE-smith and SETA.
The default UI shows source, bootstrap, instruction, Harbor and export stages;
`--no-ui` and `--json` provide durable machine-readable progress.

Inputs are merged public GitHub PRs and an explicit Python source/test profile.
All target code and image builds run remotely. Dependencies are installed during
build; task execution is offline. Source paths must identify existing Python
files or directories. Added/deleted source files and other languages need another
artifact collection profile and currently produce a recorded skip.

The reference restores the PR head's changed source files. The learner starts at
the head with those changes reversed, without Git history or the private tests.
It submits allowed Python source files into a fresh verifier environment.

`force_generate_instruction` is the upstream option for bypassing its complexity
filter. It does not bypass healthy-head, contrast or Harbor execution checks. The
20-task campaign may use it to measure generation from small functional changes.
An exported task is a generated artifact, **not quality acceptance**. Independent
reviews, attack checks and model rollouts are deferred until all recipe campaigns
reach 20 generated tasks. The current integration has not completed that review.

Credit: [SWE-gen](https://github.com/abundant-ai/SWE-gen), Apache-2.0, commit
`14e185f413f7bff03f8f9fec6fb246681bf61d74`. See [RFC 0015](../rfcs/0015-swe-gen-recipe.md)
and the packaged `recipes/swe_gen/provenance.md` for the source map and deviations.
