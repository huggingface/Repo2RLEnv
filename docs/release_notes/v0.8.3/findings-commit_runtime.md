# Arc 3 — `commit_runtime` sweep findings

**Scope.** Commit-level mining (SWE-Gen-style). This arc lands two
optimizations from plan §4 Arc 3; the full 38-repo sweep is deferred
pending cost approval (envelope ~$80-250).

## Optimizations landed

### 1. Bot / chore / merge commit filters

Plan §4 Arc 3 issue (b): commit_runtime currently feeds every merged
commit through the (expensive) validation harness, including bot bumps
and merge commits that never have real bug-fix signal. Adds two cheap
metadata-level filters:

- **`_looks_like_bot_author`** — substring check on `author_name +
  author_email` for `[bot]`, `dependabot`, `renovate`, `pre-commit-ci`,
  `github-actions`, `snyk-bot`, `greenkeeper`. Surfaces as
  `skip_reason=bot_author`.
- **`_looks_like_chore_subject`** — anchored regex on the commit subject
  for routine maintenance shapes: `chore:` / `chore(deps):` /
  `build(deps)` / `Merge pull request` / `Merge branch` / `bump <pkg>` /
  `release vX` / `[skip ci]` / `version bump`. Surfaces as
  `skip_reason=chore_message`.

Both gated by new `CommitRuntimeOptions` flags
(`skip_bot_authors=True`, `skip_chore_messages=True`, both default
enabled). Disable per-arc when a repo's convention legitimately uses
one of these tokens (e.g. `chore-board` as a directory name).

**Why anchored**: a substring regex would mis-fire on
`"Fix the chore-handling code path"`. We use `re.match` against the
subject line start so only true conventional-commit prefixes / merge
boilerplate / squash markers are caught.

### 2. Inherit F2P relaxation from pr_runtime

Adds `CommitRuntimeOptions.allow_no_f2p_with_test_patch` and reuses
`pr_runtime._should_skip_no_f2p` for the gate. Both pipelines now share
the same F2P semantics + opt-in relaxation. Set via
`--pipeline-opt allow_no_f2p_with_test_patch=true` to accept commits
where the modified tests already passed before the fix.

## Test coverage

7 new unit tests added in `tests/test_pipeline_commit_runtime.py`:

| Test | Covers |
|---|---|
| `test_looks_like_bot_author_by_name` | author_name match |
| `test_looks_like_bot_author_by_email` | author_email match (no-reply suffix) |
| `test_looks_like_bot_author_real_human_passes` | no false-positive on human names |
| `test_looks_like_chore_subject_positive` | 9 chore-style subjects all match |
| `test_looks_like_chore_subject_negative` | 4 legitimate-looking subjects all pass through |
| `test_metadata_filter_dispatch_via_class_method` | filter routes bot → `bot_author`, chore → `chore_message`, real fix → None |
| `test_metadata_filter_keeps_legacy_behavior_when_flags_off` | turning both flags off reverts to v0.8.1 behavior |

Full suite at **665 passing**, lint + format clean.

## Acceptance criteria check (this PR)

| Gate | Target | Actual | Pass? |
|---|---|---|---|
| ≥ 1 optimization landed | yes | 2 optimizations: bot/chore filters + F2P relaxation inherit | ✓ |
| Existing tests stay green | 100% | 665/665 + 2 skipped | ✓ |
| Lint + format | clean | clean | ✓ |
| Full 38-repo sweep | yes | **deferred — pending user OK on cost** | — |
| HF dataset published | yes | **deferred — once full sweep lands** | — |

## What's pending for full completion of this arc

1. **Full sweep across all 38 repos** — should yield ~55 verified envs
   per plan §0. Cost envelope ~$80-250 (each commit needs a 2-stage
   Docker validation; commit_runtime emits more candidates than
   pr_runtime per repo since it walks `git log` directly).
2. **HF push** to `AdithyaSK/repo2rlenv-v083-commit_runtime`.
3. **Findings update** with concrete T2/T3 numbers + the
   bot-filter-impact metric (count `bot_author` + `chore_message` skip
   reasons in the sweep aggregate).

## Out of scope for this arc

- Instruction-synthesis prompt quality (plan §4 Arc 3 issue (a)) —
  defer to v0.9; the v0.8.3 default uses raw commit subject+body which
  is sufficient for the launch.
- Per-language log-parser polish — same status as Arc 2; needs real
  failure modes from Tier C cells.
