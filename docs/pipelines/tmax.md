# `terminal_synth / tmax`

TMax composes terminal skills into new tasks and checks that the starting
environment contains the fixtures described by the task.

```text
Seeded native taxonomy sampler → task template + private ground truth
                             → initial-state tests
                             → final-state tests
                             → environment + reference
                             ↺ fresh initial-state execution and fixture repairs
                             → Harbor baseline/reference → generated task
```

Run `repo2rlenv generate --config examples/owned-tmax.yaml`. The seed file is a
JSON sampler configuration, for example:

```json
{"corpus_kind":"legacy","count":40,"seed":24,"domains":["data_processing","file_operations","data_querying"],"languages":["Python","Bash"]}
```

Omit `domains` or `languages` to retain the full corresponding taxonomy. The
sampler preserves the upstream legacy axes and weighted language selection;
restrictions select a conditional subset. Configuration and sampled axes are
recorded with the generated artifacts. Options include `target`,
`max_candidates`, `max_repairs`, `seed`, `test_timeout_sec` and `max_tokens`.

This first profile generates text fixtures in a single CPU Docker container.
All dependency installation happens during image build. The solver runs as
`user`, without runtime internet, and can edit `/workspace` and `/home/user`.
Initial-state tests, final-state tests and the reference stay outside the
learner image. The builder receives execution errors for bounded repairs.

The campaign target is **20 generated tasks**. It runs the native initial-state
check and a fresh Harbor baseline/reference pair. Detailed reward-hack review,
blind solver trials and acceptance follow after all recipes reach their
generation targets. TMax v2 multimodal fixtures, metric verifiers and the
upstream large sampled-solution stage are outside this first profile.

Use the [shared worker, budget and progress interface](owned_recipes.md) for
Modal or Daytona. Credit: [TMax](https://github.com/hamishivi/tmax), Apache-2.0,
commit `7387d2f9142397a458dc39f0827a2ab0b4c03cda`. See
[RFC 0018](../rfcs/0018-tmax-recipe.md) and packaged
`recipes/tmax/provenance.md` for retained files and adaptations.
