# Recorded deviations and export mapping

Run the original procedural generator and Docker validation CLI inside the remote
Modal VM. The separate upstream Modal campaign script defaults to very large worker
counts and a 50-patch threshold; it is not necessary to run these native entry points.
The smoke uses the supported `mewwts__addict.75284f95` profile, existing upstream
image, original mutation operators, random seed 24, and bounded candidate counts.

Issue generation preserves `ig_v2.yaml` prompts and its GPT-5-mini model, using direct
OpenAI authentication instead of Portkey. `001-issue-resource-limit.patch` caps the
completion at 4096 tokens and records the original response/usage.

The upstream gather command publishes branches into its mirror. This reproduction
instead projects the native validation records into a local instance file for its
original issue writer and Harbor export. At this pin `valid.py` already reports
FAIL_TO_PASS from buggy pre-gold to healthy post-gold; those values are preserved.
The older gather implementation expects the opposite orientation, so it is not used
as an acceptance authority. Native acceptance means the validator reports at least
one FAIL_TO_PASS test without timing out.

The Harbor adapter pins the pulled healthy image by digest, applies the original
mutation, copies the issue text, and uses the inverse mutation as the reference.
It restores the original F2P/P2P test files at verification, matching upstream's
evaluation protection against test edits, then runs the profile's original test
command. Python pytest status parsing and PASSED/XFAIL handling follow the pinned
native grader. Conversion parity must be tested independently.

Explicit export isolation changes: remove workspace git metadata and disable internet
in solver containers. The original images and validation run retain their native
configuration. No upstream repositories or Docker registries are written to.
