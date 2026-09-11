# `env_repair / cli_gym`

CLI-Gym derives repair tasks by deliberately breaking a healthy development
environment and verifying that a recovery restores its existing tests.

```text
Pinned repository → healthy image + nonempty test identities
                  → sampled tests + disruption direction
                  → inversion goal → destruction/recovery scripts
                  ↺ execute disruption, observe failures, execute recovery
                  → symptom-based repair instruction
                  → independent Harbor rebuild + baseline/reference → export
```

Run `repo2rlenv generate --config examples/owned-cli-gym.yaml`. This first profile
supports public GitHub Python repositories with a `tests/` directory. The
configuration supplies source paths, test/build dependencies, offline recovery
assets and optional disruption directions. The target repository is cloned in
the remote bootstrap; the CLI-Gym research repository is never a runtime dependency.

The inversion author samples up to fifty real passing test identities, proposes
a distinct environment failure, then revises its scripts using actual execution
feedback. Source and test files are protected. Changes must persist in files;
temporary shell exports cannot represent a separate environment state.

The baseline must lose at least one selected test and the recovery must restore
all required healthy tests. Empty, malformed and incomplete results cannot earn
success. A broken Python import or collection step is a valid environment
failure, but is reported accurately. The final Harbor bundle undergoes another
fresh baseline/reference pair before export.

The solver repairs the container as root, offline, without changing repository
source or tests. The example makes dependency wheels available in `/opt/wheelhouse`.
The build-time destruction script stays outside the learner filesystem; its
inverse remains a private reference. Full adversarial review of the root runtime
is deferred to the later quality campaign.

Options include `target`, `max_candidates`, `max_rounds`, `seed`, `directions`
and the common Python build/test profile. The first target is **20 generated
tasks**, using the [shared Modal/Daytona and progress interface](owned_recipes.md).

Credit: [CLI-Gym](https://github.com/LiberCoders/CLI-Gym), MIT, commit
`48bb920b728a25a55a5b442303e901919654599e`. See
[RFC 0021](../rfcs/0021-cli-gym-recipe.md) and packaged
`recipes/cli_gym/provenance.md` for source mapping and profile restrictions.
