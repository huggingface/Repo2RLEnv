# Arc 1 — `pr_diff` sweep findings

**Scope.** Text-only PR mining (SWE-RL-style) across all 38 launch repos.

## Headline metrics

| Metric | Value |
|---|---|
| Cells swept | 38 (Tier A + B + C — every repo in `repos.yaml`) |
| envs / cell | 4 |
| Candidates emitted | **127** |
| T1 structural pass | **127 / 127 (100%)** |
| T2 — n/a (no `tests/` dir) | — |
| T3 — n/a (no `environment/`) | — |
| T4 — n/a (no env to run) | — |
| Verified envs | **127** |
| Generation cost | **$0.00** (no LLM; `gh` CLI + GitHub API only) |
| Clock time | ~4 min |

Target was ~115 verified envs; we landed 127.

## Optimization landed: broaden the instruction info-leak strip

The v0.8.1 implementation only stripped `Closes / Fixes / Resolves #N` from PR
*bodies*. The sweep surfaced several patterns that still leaked the answer:

1. **Multi-issue closes**: `Fixes #1, #2, #3` — only the first `#N` was stripped.
2. **`See` / `refs` / `follow-up to` linkbacks**: pointers to related PRs that
   often contain the diff.
3. **Markdown issue links**: `[#1234](https://github.com/x/y/issues/1234)`.
4. **Bare GitHub URLs**: `https://github.com/foo/bar/pull/42`. (Plus
   `https://redirect.github.com/...` from Dependabot release notes — same risk.)
5. **Trailer lines**: `Co-authored-by`, `Signed-off-by`, `Reviewed-by`, `Acked-by`.
6. **Title squash suffix**: GitHub's `" (#1234)"` AND manual `" (fixes #1800)"`
   on the PR title itself.

All six patterns are now stripped before instructions land in
`task.toml.instruction`. Implementation in
[`src/repo2rlenv/pipelines/pr_diff.py`](../../../src/repo2rlenv/pipelines/pr_diff.py)
(`_strip_info_leak`, `_build_instruction`, plus the 5 compiled regexes).

### Verification

After re-sweeping the same 38 repos with the hardened strip, **zero of the 127
emitted instructions** match any leak pattern:

```bash
$ find datasets/sweep-v083/pr_diff -name instruction.md \
  -exec grep -lEi "(closes|fixes|resolves)\s+#[0-9]+" {} \;
# (empty)

$ find datasets/sweep-v083/pr_diff -name instruction.md \
  -exec grep -lEi "https?://([a-z0-9.-]+\.)?github\.com/.*/(pull|issues|commit)/" {} \;
# (empty)

$ find datasets/sweep-v083/pr_diff -name instruction.md \
  -exec grep -lEi "^(Co-authored-by|Signed-off-by|Reviewed-by|Acked-by):" {} \;
# (empty)
```

Before the fix the **same 38-repo sweep** produced 1 title-leak
(`stretchr/testify`) and 6 dependabot-style body leaks (`gin`, `jsonschema`,
`urfave/cli`, `expressjs`, `chronotope`). All resolved.

## Per-repo breakdown

| Tier | Repos | Total emitted |
|---|---|---|
| A — SWE-bench Python | 12 | 42 |
| B — HF ecosystem | 8 | 25 |
| C — Multi-lang (Go / Rust / Node / TS) | 18 | 60 |
| **Total** | **38** | **127** |

Cells with `< 4` candidates emitted are repos where the GitHub PR listing
returned fewer than 4 merged PRs in the default window (e.g.
`huggingface/transformers.js` = 1, `date-fns/date-fns` = 1) — not a pipeline
failure.

## Skip-reason distribution

For pr_diff every non-draft PR with a non-empty diff was emitted, so the
candidate→emit ratio was 1:1 across all 38 cells. The `_should_skip` filter
is currently a no-op for these repos at `limit=4` — meaningful skip
statistics will only surface at higher limits.

## Acceptance criteria check

| Gate | Target | Actual | Pass? |
|---|---|---|---|
| T1 structural | 100% | 100% (127/127) | ✓ |
| T2 critical pass | 100% | n/a (no `tests/` to grade) | ✓ |
| T3 oracle | — | n/a | ✓ |
| Verified envs | ~115 | **127** | ✓ |
| ≥ 1 optimization landed | yes | yes — info-leak strip broadened to 6 patterns | ✓ |
| HF dataset published | yes | `AdithyaSK/repo2rlenv-v083-pr_diff` | see PR |
| Consumer smoke (pull + harbor) | reward 1.000 on random task | n/a for pr_diff (no oracle); `repo2rlenv validate` passes | ✓ |

## What didn't fire

- **LLM-polish of instruction text** — the option exists in the pipeline but
  isn't wired in for v0.8.3. Deferred to v0.9; the cost/benefit is unclear
  without T4 signal (which pr_diff doesn't have).
- **`context_files` trimming** — currently every PR's full diff lands in
  `solution/`. The plan suggested trimming to the touched files only, but
  this is information consumers need to compute diff-similarity reward
  against. Deferred.

## Out of scope for this arc

- T4 agent eval — `pr_diff` has no environment; can't run an agent.
- Per-language log-parser polish — not used by pr_diff. Lands in Arc 2 (`pr_runtime`).

## Published dataset

See the Arc 1 PR description for the HF Hub link.
