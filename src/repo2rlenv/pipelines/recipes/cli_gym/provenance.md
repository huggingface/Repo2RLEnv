# CLI-Gym adaptation

Source: [LiberCoders/CLI-Gym](https://github.com/LiberCoders/CLI-Gym), commit
`48bb920b728a25a55a5b442303e901919654599e`, MIT. `UPSTREAM_LICENSE` retains
the original license. `inversion_prompt.md` and `instruction_prompt.md` retain
the templates under `CLI-Gym/build_destruction_task/` and
`CLI-Gym/assemble_problem_instance/`.

The owned recipe follows gold-environment construction, sampling up to fifty
healthy test identities, disruption-goal generation, execution-guided environment
inversion, and symptom-based repair-task assembly. It shares the repository's
remote bootstrap instead of requiring an installed SWE-smith registry. The
native default directions can be supplied explicitly through `directions`.

The first profile supports public Python repositories with a `tests/` directory.
The author produces reproducible destruction and recovery bash scripts, then
receives actual test and restoration results for bounded revisions. This is a
structured command loop rather than an imported OpenHands/Terminus runtime.
It supports persistent filesystem-based environment changes; repository source,
tests and the isolated control interpreter must survive inversion unchanged.
CPU/memory/PID limits and offline containers bound execution. Kernel changes,
multi-service scenarios and resource-exhaustion tasks are outside this profile.

Unlike the earlier native pilot, empty or malformed test results never pass.
The healthy test suite must contain passing identities, the broken environment
must lose at least one selected identity, and recovery must restore every
required healthy identity. A collection failure is recorded as a collection
failure, not fabricated assertion evidence. Harbor independently rebuilds the
export and checks baseline zero/reference one before generation succeeds.

The export's Dockerfile copies only the repository source subdirectory, keeping
the build-time inversion commands outside the learner filesystem. Reference and
trusted original tests are private Harbor assets. The grader also requires
repository source hashes to remain unchanged. The current root environment-repair
profile still needs the later adversarial runtime audit; these generation checks
do not imply training approval. Offline recovery assets are prepared by the
explicit repository install profile, normally as wheels in `/opt/wheelhouse`.

No research project is installed, imported or cloned at runtime. The target
repository is the input, with a recorded revision and bootstrap configuration.
