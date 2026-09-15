# TMax adaptation

Source: [hamishivi/tmax](https://github.com/hamishivi/tmax), commit
`7387d2f9142397a458dc39f0827a2ab0b4c03cda`, Apache-2.0. `UPSTREAM_LICENSE`
retains the original license. The taxonomy and prompt text are adapted from
`rl_data/generator/task_template_gen.py`, `initial_state_test_gen.py`,
`completion_test_gen.py` and `apptainer_def_gen.py` at that revision.

The owned recipe follows the legacy branch of `rl_data/generate_tasks.py`:
sample domain, skill type, three to five primitive skills, task complexity,
command complexity, scenario and a weighted language; generate a task template
and private ground truth; separately generate initial-state and final-state
tests; construct the environment; execute the initial-state check; repair from
execution feedback. The native real-software anchor probability is 0.35.
The retained taxonomy has nine domains and eight language choices. Optional
domain/language restrictions use rejection sampling, with a deterministic seed
and an explicit draw limit. They define a supported campaign profile rather
than silently changing the native weights.

Structured JSON replaces upstream XML and code-fence extraction. Text fixtures
and Dockerfile setup instructions replace Apptainer definition files. The
initial profile is a single offline CPU Linux container with a non-root solver;
dependencies are installed during image build. Initial tests run in a separate
fresh Harbor trial and never enter the learner image. Final tests have five to
ten named pytest functions and explicit weights. A generated reference is
checked with Harbor along with the unsolved starting state.

The v2 multimodal fixture generator, its extra taxonomy axes and metric-verifier
modes are not implemented in this profile. Upstream's large sampled-solution
stage is deferred to the campaign's later rollout phase. A successful generation
receipt is not a replication of those solution statistics or quality acceptance.
No upstream research repository is installed, imported or cloned at runtime.
