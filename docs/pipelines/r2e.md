# `equivalence_tests / r2e`

The owned R2E recipe generates differential tests from real repository functions,
repairs them using execution and coverage feedback, then refines the task's
specification using the observed behavior.

```text
Pinned healthy repository → function and dependency slice
                        → generated differential unittest
                        → execution + generated-test branch coverage
                        ↺ bounded test repairs
                        → observed behavior → refined docstring and task
                        → private Harbor verifier → baseline/reference → export
```

Use `repo2rlenv generate --config examples/owned-r2e.yaml`. The existing native
`equivalence_tests` pipeline keeps its original options and behavior; selecting
`recipe: r2e` uses this owned execution loop and its separate options. Cloud
workers, budget accounting, resume receipts and Rich/JSON progress follow the
[shared interface](owned_recipes.md).

The first profile requires a working public GitHub Python repository with a
`tests/` directory. It selects documented module-level synchronous functions and
includes their bounded module-level dependency context. Classes, async functions,
nonstandard source roots and reconstructed cross-module import slices require
another extraction profile.

Generated tests use the native `function` / `reference_function` API. The reference
and test bindings are added only to the separate verifier image; the learner sees
a stub in the real repository and the refined requirements. Coverage is collected
only while generated tests execute. Existing repository tests run as well, without
contributing to that coverage score. The default is three rounds and 80% branch
coverage. These are native generation controls, separate from later quality review.

The current verifier loads the private Python reference in the same process as
the differential tests. Reference access during adversarial grading is still part
of the deferred audit; no quality-accepted claim is made. The first campaign aims
for **20 generated tasks** with fresh Harbor baseline/reference checks.

Options extend the Python source/build/test profile with `target`,
`max_candidates`, `seed`, `max_rounds` and `min_branch_coverage`. The default build
dependencies include pytest and Coverage.py; explicit dependency overrides must
include those libraries.

Credit: [R2E](https://github.com/r2e-project/r2e), MIT, commit
`bcbed156711bb939de14aa46b27eee15073f5272`. See [RFC 0017](../rfcs/0017-r2e-recipe.md)
and the packaged `recipes/r2e/provenance.md` for the source map and adaptations.
The execution report uses Coverage.py's [branch measurement](https://coverage.readthedocs.io/en/latest/branch.html)
and [JSON reporting](https://coverage.readthedocs.io/en/latest/commands/cmd_json.html).
