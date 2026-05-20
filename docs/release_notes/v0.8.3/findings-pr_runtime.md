# Arc 2 — `pr_runtime` sweep findings

**Scope.** SWE-bench-style PR mining with sandbox-verified F2P/P2P sets.
This arc lands the **optimization + harness bugfix**; the full 38-repo
sweep is deferred until the user signs off on the Docker / LLM cost
envelope (plan §6: ~$80-250).

## Optimization landed: opt-in F2P filter relaxation

Plan §4 Arc 2 known issue (b): ~17-27% of candidates drop on
`no_fail_to_pass` because the PR's test_patch added or modified tests
that already passed before the fix landed. These PRs are real production
bug fixes — they merged for a reason — but their test coverage doesn't
follow the strict "this test fails on base, passes after patch" SWE-bench
shape.

**Fix.** Add `PRRuntimeOptions.allow_no_f2p_with_test_patch` (default
`False` — preserves the strict v0.8.1 behavior). When set via
`--pipeline-opt allow_no_f2p_with_test_patch=true`, the pipeline accepts
candidates with empty F2P **iff** the test_patch is non-empty AND the
P2P set has at least one entry. The verifier still gates on post-patch
behavior, so a no-op model patch would have to pass the modified tests too.

The decision now lives in a pure helper, `_should_skip_no_f2p`, with 6 new
unit tests covering the strict path + each relaxation guard.

## Harness fix: don't fail cells that emit 0 tasks

The smoke surfaced a separate, important issue in the sweep driver:
`repo2rlenv generate` exits 1 when `emitted=0` (see `cli.py:275`).
For `pr_runtime` that is a normal filter outcome — many PRs are
correctly skipped on `no_test_patch` / `no_fail_to_pass`. The sweep
was misreading that as a cell failure.

Fix in `scripts/v083/sweep.py`: when the subprocess exits non-zero AND
the `out_dir` was created AND no task directories landed, treat the
cell as `done` with `candidates=0`. True crashes (out_dir missing) still
land as `step=failed`. 3 new unit tests on the option parser, suite at
**658 passing**.

Also exposed `--pipeline-opt KEY=VALUE` on `sweep.py` so per-arc options
(`allow_no_f2p_with_test_patch=true`, `limit=...`, etc.) can be passed
without editing the script.

## Smoke evidence (limited)

| Sweep | Repos | envs/cell | Outcome |
|---|---|---|---|
| #1 (pre-harness-fix) | pallets/click | 2 | sweep reported `step=failed` because of the `emitted=0 → exit 1` issue; pipeline itself worked (2 candidates, both filtered). Surfaced the harness bug. |
| #2 (direct, with flag) | pallets/click | 3 | 3 PRs: 2 `no_test_patch`, 1 `no_fail_to_pass` (P2P also 0 — relaxation correctly didn't rescue) |
| #3 (post-fix, with flag) | pallets/click, pallets/werkzeug | 5 each | 2/2 cells `step=done`, 0 candidates emitted, $0 cost. Harness fix verified. |

The 10 most-recent PRs in `pallets/click + pallets/werkzeug` happen to
not include any that match the relaxation pattern (F2P=0 +
test_patch≠"" + P2P≥1). To exercise the relaxation rescue path requires
either historical PRs (set `--pipeline-opt since=2024-01-01`) or
higher-activity repos like `pydantic/pydantic` or `psf/requests`. Both
are deferred to the full sweep.

## Acceptance criteria check (this PR)

| Gate | Target | Actual | Pass? |
|---|---|---|---|
| ≥ 1 optimization landed | yes | F2P-relax flag + `_should_skip_no_f2p` helper + 6 tests | ✓ |
| ≥ 1 harness bugfix from sweep | — | exit-code-1-with-emitted-0 → `done` not `failed`; `--pipeline-opt` plumbed | ✓ |
| Existing tests stay green | 100% | **658/658** + 2 skipped | ✓ |
| Lint + format | clean | clean | ✓ |
| Smoke against ≥ 1 cached-bootstrap repo | yes | pallets/click + pallets/werkzeug | ✓ |
| Full 38-repo sweep | yes | **deferred — pending user OK on cost** | — |
| HF dataset published | yes | **deferred — once full sweep lands** | — |

## What's pending for full completion of this arc

1. **Full sweep across all 38 repos** — should produce ~75-85 verified
   envs per plan §0. Cost envelope per plan §6: ~$80-250 (bootstrap
   LLM for any uncached repo + T2 Haiku + optional T4 Sonnet). Wall
   time: 6-12 h.
2. **HF push** to `AdithyaSK/repo2rlenv-v083-pr_runtime` once the
   verified set is ready.
3. **Findings update** with concrete T2/T3 numbers + the
   optimization-impact diff (candidate yield with
   `allow_no_f2p_with_test_patch=true` vs `False`).
4. **Per-language log-parser polish** for Tier C Go/Rust/Node/TS repos —
   listed as the secondary optimization in the plan. Deferred to the
   full-sweep iteration since we need real failure modes from Tier C
   cells to prioritize parser changes.

## Out of scope for this arc (deferred to v0.9)

- LLM-judged QA gate (SWE-Bench++ recipe).
- Per-PR test-cmd normalization edge cases beyond what #23 already shipped.
- Targeted-test command building improvements for unusual `pytest`
  invocation patterns.
