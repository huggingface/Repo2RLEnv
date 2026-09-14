# RFC 0029: Tasksmith repository bootstrap and resource profiles

**Status:** implemented for the profiles described in the [Tasksmith guide](../pipelines/tasksmith.md).

## Problem

A merged PR may need a different dependency snapshot, test selection or GPU count
from another PR in the same repository. Building the package once does not prove
that its tests run offline or that the filtered learner workspace still installs.
Repeatedly rebuilding identical dependencies also wastes time and compute.

## Decisions

The investigator supplies a typed profile from the pinned source: dependency
inputs, install commands, private test paths, test selectors and CPU/GPU rationale.
Missing readiness files are rejected before remote builds. Future private tests
belong in task design rather than a fabricated bootstrap-readiness path.

Use the existing bootstrap implementation. Check both the merged repository and
the filtered learner source, including build metadata and necessary public files.
Run selected tests offline and verify that imports resolve to the intended source.
Dependency readiness and task-specific fail-to-pass contrast remain separate gates.

A source-independent dependency prefix can be reused on the same provider worker.
Its identity depends on the base, dependency inputs and installation settings;
the PR's source snapshot and readiness evidence remain separately pinned. Saved
provider snapshots require an identity, expiry and a successful restore check.
They are account-scoped caches, not portable public Docker images.

## Resource boundary

CPU trials use the owned remote worker contract on Daytona or Modal. The
implemented GPU route uses native Modal allocations with one or two L4 GPUs,
with matching learner and verifier resources. A CPU inspection worker may prepare
source metadata, but it must not execute a requested GPU trial or claim GPU readiness.
Unsupported configurations fail explicitly before dispatch.

The controller performs orchestration and artifact handling; target builds,
imports, tests and Docker commands execute remotely. Reserve resource cost before
creating a worker. Preserve ambiguous allocation receipts until reconciled and
record termination instead of treating missing usage evidence as free compute.

## Validation and limitations

Bootstrap must preserve private/public source boundaries, import origin, offline
test execution and requested resource counts. Cache restoration must be checked,
not inferred from a key existing. A successful bootstrap is not an accepted RL task.
Source/runtime mismatches return to bounded profile correction; failed attempts
remain part of measured cost.

The original campaign snapshots are retained outside the public docs. Current
[datasets](../pipelines/releases.md) and [economics](../pipelines/economics.md) expose
aggregate outcomes without requiring a private campaign directory.

See [RFC 0028](0028-tasksmith-pr-pilot.md) for task construction and
[RFC 0027](0027-harbor-quality-loop.md) for independent quality checks.
