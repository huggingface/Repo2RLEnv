# RFC 0029: HF repository bootstrap and bounded Tasksmith scale

Status: implementing.

Use the supplied 114-PR HF candidate list. First freeze the six repository revisions and demonstrate CPU and GPU-host readiness. Then generate at least ten faithful Harbor environments from the candidate pool, expanding toward fifty only within the existing campaign budget and measured yield. Unsupported inputs and failed attempts remain visible.

## Milestones and evidence

1. Freeze all candidate URLs, inspect PR source changes, and record repository revisions before paid execution. Keep the original denominator separate from the selected generation panel.
2. Build CPU repository images remotely through the existing bootstrap implementation. Exercise small real operations offline, record dependency versions, and create source-free dependency hints for later PR builds.
3. Build corresponding CUDA images remotely and run real operations on a GPU. Tokenizers remains a CPU implementation; test it on the GPU host without calling it GPU accelerated. Readiness is scoped to these checks, not full upstream suites or distributed backends.
4. Preserve CPU Docker caches in a provider snapshot and verify restoration. Tasksmith can consume the snapshot and recorded dependency hints but must rebuild/retest each pinned PR. Final task Dockerfiles retain a portable install recipe rather than requiring a private local image tag.
5. Freeze an initial ten-PR CPU panel with a measured budget cap. Run existing generation, review/repair, semantic controls and Sonnet rollouts. Retain actual merged PR references. Repair orchestration defects when evidence identifies them.
6. Report generated and usable counts, first-attempt and repaired yield, stage costs, cached work, and remaining limitations. Expand in small additional panels only when the remaining budget covers their worst-case reservations.

## Resource and spending policy

The existing campaign ledger had $253.313684 available at intake. Do not reset its cap or erase uncertain reservations. Initial working allocation: at most $30 for bootstrap/build/GPU checks and $140 for the first generation panel, leaving roughly $83 for repairs or measured expansion. Reallocate within the unchanged campaign cap if evidence warrants it. Reserve before remote effects; stop workers and record conservative compute estimates after termination. Model estimates are separate from provider invoices.

All target execution is remote. Local operations are limited to metadata, orchestration, artifact serialization and tests of Repo2RLEnv code. No local target imports, Docker builds or rollouts.

## Verified provider contracts

Modal VM Sandboxes support Docker and include its state in filesystem snapshots, but currently do not support GPUs. GPU readiness therefore uses native GPU sandboxes rather than setting a GPU flag on a VM worker. [Modal VM documentation](https://modal.com/docs/guide/vm-sandboxes).

Filesystem snapshots have a default 30-day TTL; record their identity and retention and check actual restoration. They are account-scoped caches, not portable public registry images. [Modal snapshots](https://modal.com/docs/guide/sandbox-snapshots).

Daytona supports GPU sandboxes through explicit resources and GPU types. Its availability/quota must be checked before choosing it for live GPU execution. Existing CPU generation supports both providers. [Daytona sandbox documentation](https://www.daytona.io/docs/sandboxes).
