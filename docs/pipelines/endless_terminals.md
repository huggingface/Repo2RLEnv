# `terminal_synth / endless_terminals`

Endless Terminals generates terminal tasks from a category, complexity and
scenario. Its native category list covers files, text, databases, configuration,
software tools and other terminal workflows.

```text
Category + complexity + scenario → template + private ground truth
                               → initial-state tests
                               → completion tests using the initial tests
                               → environment + reference
                               ↺ initial-state execution and repairs
                               → Harbor baseline/reference → export
```

Run `repo2rlenv generate --config examples/owned-endless-terminals.yaml`.
The input is a sampler JSON file:

```json
{"count":40,"seed":24,"categories":["text processing and manipulation","backup and archiving","SQLite database operations via CLI"]}
```

Omit `categories` to sample the full native list. All three axes use uniform
sampling, preserving the original method. TMax's separate recipe uses its own
domain/skill taxonomy and language weights. They share the metered authoring
and remote execution stages.

The first profile uses text fixtures in an offline CPU Docker container. The
solver runs as `user`; dependencies are installed at build time. Initial tests
execute on a fresh environment before the final baseline/reference pair.
The builder can repair inconsistent fixtures or a failing reference within
`max_repairs`. Every attempted input and execution outcome remains in the run.

The target is **20 generated tasks**, with detailed quality evaluation and
blind rollouts deferred. Upstream's sampled solutions and training run are
outside this generation milestone. Options and cloud setup follow the
[shared interface](owned_recipes.md); the configuration records the actual
author model rather than claiming the original Qwen settings.

Credit: [Endless Terminals](https://github.com/kanishkg/endless-terminals),
Apache-2.0, commit `99f4c74b75faacf21e53d3dc01df170902e924cb`.
See [RFC 0020](../rfcs/0020-endless-terminals-recipe.md) and packaged
`recipes/endless_terminals/provenance.md` for the source map and adaptations.
