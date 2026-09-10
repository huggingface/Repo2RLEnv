# Endless Terminals adaptation

Source: [kanishkg/endless-terminals](https://github.com/kanishkg/endless-terminals),
commit `99f4c74b75faacf21e53d3dc01df170902e924cb`, Apache-2.0. The original
license is retained as `UPSTREAM_LICENSE`.

`taxonomy.json` retains `TASK_CATEGORIES`, `COMPLEXITY_LEVELS` and
`SCENARIO_CONTEXTS` from `generator/task_template_gen.py`. The sampler follows
its independent uniform choices, with a local seeded RNG instead of global
random state. An optional explicit category subset restricts the campaign
profile. `template_prompt.md`, `initial_prompt.md`, `final_prompt.md` and
`environment_prompt.md` retain the respective `SYSTEM_MSG` values from
`task_template_gen.py`, `initial_state_test_gen.py`, `completion_test_gen.py`
and `apptainer_def_gen.py`.

The owned pipeline preserves the generation order in `generate_tasks.py`:
template with privileged truth; initial tests; completion tests conditioned
on the initial tests; environment construction with initial-test feedback.
Shared `terminal/templates.py` implements these stages for this method and
TMax, which derives a related construction loop but has its own different
sampler and prompts. This recipe does not route its inputs through TMax's
skill taxonomy.

The runtime uses structured JSON and remote Docker rather than XML/fenced
responses and local Apptainer. The initial profile supports text fixtures in
one offline CPU container with a non-root solver. A generated bash reference,
five to ten named completion tests and weighted Harbor rewards provide the
common export contract. Initial tests run separately and all tests/reference
files remain outside the learner image.

Upstream Qwen model settings are replaced by the explicitly configured provider
and model, recorded in the run. The large sampled-solution generation and
training stages are deferred to later evaluation, not claimed as reproduced
by exporting a task. No upstream research package is installed or imported.
