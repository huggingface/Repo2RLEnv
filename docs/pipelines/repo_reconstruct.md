# `repo_reconstruct / swe_flow`

SWE-Flow creates reconstruction tasks from a working repository's execution
dependencies. It does not need PR history.

```text
Pinned repository → cached healthy image → bounded test-call traces
                 → group shared dependencies → order development steps
                 → stub entry points / remove new helper implementations
                 → test contrast → generated docstrings + requirements
                 → Harbor baseline/reference → generated task
```

Run `repo2rlenv generate --config examples/owned-swe-flow.yaml`. Cloud workers,
campaign budgets, resume receipts and the Rich/JSON CLI use the
[shared owned recipe interface](owned_recipes.md).

The first profile supports top-level synchronous Python functions in supplied
source paths. Tracing samples 128 tests with seed 24 by default. Each traced test
has a five-second profiling window; incomplete traces do not enter scheduling.
The healthy and skeleton states still run the complete configured test suite.
Additional threads, subprocesses, classes and asynchronous functions require
another tracing profile.

Tests with the same observed function dependencies form a group. Groups are
ordered by dependency count; each step introduces only previously undeveloped
functions. Entry points retain their signatures and receive generated behavioral
docstrings. Newly introduced helper implementations are removed. The reference
restores the original source. The remaining repository supplies realistic context.

Two separate model stages generate function docstrings and test-grounded task
requirements using the upstream prompts. Fresh Harbor checks establish an
unsolved baseline and working reference. The target is **20 generated tasks**;
independent quality review, attack checks and model rollouts come later.

Options extend the Python build/source/test profile with `target`,
`max_candidates`, `trace_max_tests` and `trace_seed`. Every trace, schedule, source
snapshot, model request and trial stays in the campaign evidence directory.

Credit: [SWE-Flow](https://github.com/Hambaobao/SWE-Flow), MIT, commit
`7da5b046fa1dc184674e4e94a9989be56c39e4e7`, and
[SWE-Flow-Trace](https://github.com/Hambaobao/SWE-Flow-Trace). See
[RFC 0016](../rfcs/0016-swe-flow-recipe.md) and the packaged
`recipes/swe_flow/provenance.md` for implementation differences.
