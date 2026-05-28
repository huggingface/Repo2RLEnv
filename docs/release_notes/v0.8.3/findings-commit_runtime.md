# `commit_runtime` — filter / leak / artifact fixes + 52-env reference dataset (Arc 3)

This release sharpens `commit_runtime` end-to-end and ships **52 oracle-verified environments** as the reference dataset. Kept **experimental** for now — the underlying mining problem (commits without linked issues yield thinner instructions than PRs do) hasn't fully gone away, but every other lever has been pulled.

## Published dataset

**<https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime>** — added to the [Verifiable RL Environments](https://huggingface.co/collections/AdithyaSK/repo2rlenv-verifiable-rl-environments-6a15e7eee7c112fe841b2990) collection.

- **52 environments**, all oracle-verified (`reward == 1.0` with the gold patch).
- Resolution split: **52 `resolved`** (tracked SWE-bench-style) · **47 `command_resolved`** (clean test command) · **47 `eval_grade`** (`command_resolved` + non-empty P2P regression guard).
- 12 source repos: `urfave/cli` 15 · `gin-gonic/gin` 8 · `pallets/click` 7 · `gorilla/mux` 6 · `python-attrs/attrs` 4 · `stretchr/testify` 3 · `spf13/cobra` 3 · `pocketbase/pocketbase` 2 · `pallets/werkzeug` · `encode/httpx` · `sirupsen/logrus` · `psf/requests` (1 each).
- **Go-heavy by design** — see Audit § Repo skew below.
- Same shape as the `pr_runtime` reference dataset (manifest with per-task build/oracle status + checksums + dataset commit; plain `tests/verifier.py` + `tests/f2p.json` + `tests/p2p.json` artifacts; inline `environment/Dockerfile` from public bases).

## What changed

`commit_runtime` already reuses `pr_runtime`'s validation harness, so most of Arc 2's wins came along for free. Arc 3 was about everything **upstream** of validation (the mining filters and the instruction text) plus one critical Arc-2 inheritance bug.

### 1. Non-bugfix conventional-commit-type rejection

Before Arc 3, `_CC_PREFIX_RE` only *stripped* the conventional-commit type from a subject. So `chore: bump version`, `docs: typo`, `feat: add X`, `refactor: rename Y`, `style: black format`, `test: cover Z`, `ci: pin actions`, `build: drop py3.10`, `perf: hot loop`, and `revert: …` all sailed past the filter, got bootstrapped, and burned validation cycles before finally getting rejected at the F2P stage. The new `_NON_BUG_TYPE_RE` returns `non_bugfix_type` from `_metadata_filter` for those, mirroring `pr_runtime._NON_BUG_TITLE_RE`.

### 2. Bugfix positive-signal filter

A commit with no type prefix had no positive bugfix signal either — so plain-subject feature commits ("Update README", "Improve performance") slipped through. Now `_metadata_filter` requires at least one of:

- `fix:` prefix
- A `Closes #N` / `Fixes #N` issue trailer
- A bugfix keyword in the subject (`fix` / `bug(fix|s)?` / `regression` / `crash(ed|ing)?` / `broken` / `incorrect(ly)?` / `wrong(ly)?` / `fail(s|ed|ing|ure)?` / `defect` / `hotfix` / `patch(ed)?`)

### 3. Issue-fetch fallback for the instruction

The biggest single quality lever, ported from Arc 2's PR-body leak fix. Commit messages routinely name the function being fixed ("Fix off-by-one in `Pager.advance`"), describe the fix approach, and link to companion fix-PRs or commit SHAs. The bug **report** doesn't usually do any of that.

When a commit has `Closes #N`, `build_instruction_from_commit` now fetches the issue body (`github.fetch_issue`, same path `pr_runtime` uses) and renders that as the problem statement. Commits without a linked issue fall back to the commit subject + body, run through `_strip_info_leak` and `_reflow_pr_body`. The smoke test on `pallets/click` showed the first emitted task's instruction came verbatim from the GitHub issue, not the commit message — exactly the design.

### 4. Solution-leak strip + extended patterns

Imported `_strip_info_leak` from `pr_runtime` and applied it to commit subject + body. Plus two new patterns covering the audit's residual soft leaks:

- **Trailing `(#NNNN)` squash trailers** — Pallets/Werkzeug/Flask/httpx all squash-merge PRs with `(#1234)` appended to the subject; that's a direct lookup back to the merged fix.
- **Cross-repo issue refs without a closes keyword** (`gorilla#739` style) — the existing `_LEAK_PATTERNS` only caught the `owner/repo#N` form when paired with a closes keyword.

Both shared with `pr_runtime` since the patterns are universally applicable.

### 5. Body reflow

Ported `_reflow_pr_body` (drop HTML template comments, stop at checklist headers, collapse blank runs, length cap) — commit bodies can be just as verbose as PR bodies, especially when authors paste in CI output or sign-off lines.

### 6. `reward_calibration` stamping (parity with `pr_runtime`)

Emitted tasks now carry `[metadata.repo2env.reward_calibration]` with `f2p_count`, `p2p_count`, `source_files`, `loc_changed`, and a bucketed `difficulty` — and the `HarborTask.difficulty` is set from the bucket rather than the hard-coded `"medium"`. The launch-side manifest enricher (`plans/build_enriched_manifest.py`) reads these to compute the `eval_grade` flag without re-parsing the diff.

### 7. The critical bug: emit graded `test.sh` + plain artifacts (Arc 2 inheritance miss)

Arc 2 refactored `pr_runtime` to ship `tests/{verifier.py,f2p.json,p2p.json}` as plain task artifacts and to call them from `test.sh`. `commit_runtime` never inherited that — it called `pr_runtime.build_eval_script` *without* `fail_to_pass` / `pass_to_pass`, which falls back to the binary exit-code path. Result: emitted `test.sh` only wrote `reward.txt` (binary 1.0/0.0), `reward.json` was **never** written, and `tracked` / `command_resolved` / the full F2P/P2P breakdown were silently lost across the whole dataset.

Two fixes:

- Pass `fail_to_pass=…, pass_to_pass=…` to `build_eval_script` so the graded path triggers.
- Pass `aux_files=_runtime_aux_files(fail_to_pass, pass_to_pass)` to the emitted `HarborTask` so the three artifacts ship with the task dir.

The oracle gate caught this immediately — `0/56 reward.json` files on the first run vs `52/52 reward.json` after the fix.

## Validation evidence

Full 100-task-style oracle gate run on the 52-env dataset:

- **52/52 tracked-resolved** (gold patch always satisfies the F2P+P2P sets — the oracle invariant).
- **47/52 command_resolved.** The 5 that aren't: 3× `spf13/cobra` (one untracked failure each — sibling test in the same file that the gold patch doesn't touch) + 2× `stretchr/testify` (22 untracked failures each — Go-subtest parser over-counts; see Limitations below).
- **47/52 eval_grade.** All 47 `command_resolved` tasks also have `p2p_count > 0`, so `eval_grade` matches `command_resolved` exactly on this dataset.

A separate qualitative audit on 9 stratified tasks (one per repo, eval_grade + edge cases): **5/9 gold-standard** (clean issue body sourced from GitHub, well-scoped F2P/P2P, real source fix), **3/9 mediocre** (thin commit-message-fallback instructions when no linked issue), **1/9 weak** (270-char title-only prompt — also one of the 4 broken-oracle tasks that was dropped pre-publish). The mediocre tail is the structural commit-runtime limitation, not a fixable bug.

**4 broken-oracle tasks dropped** from the 56 generated: `psf__requests-6404f3`, `stretchr__testify-15f682`, `urfave__cli-195aaf`, `urfave__cli-b4f42d`. All scored 0.97-0.999 on the gold patch — single flaky P2P each, same shape as Arc 2's `urfave__cli-2290`/`TestCompletionShell`. Could be salvaged with the same flaky-P2P prune Arc 2 used; left for a future polish pass.

## Limitations

- **Thin instructions when no linked issue.** ~30% of tasks have no `Closes #N` and fall back to the commit subject + body. When the commit subject is a one-liner ("Fix RFC 2069 mode digest authentication"), the agent has very little to work with. This is the structural difference vs `pr_runtime` and the reason `commit_runtime` stays experimental.
- **Go-subtest parser over-counts untracked failures.** The verifier's Go-test log parser treats each subtest line as a tracked test, so when a subtest fails inside a non-F2P parent the count of "untracked failed" balloons (22 for `stretchr/testify` even though the test command exited 0). Affects `command_resolved` / `eval_grade` flagging only; `resolved` and the graded `reward` are correct. Subtest-aware grouping is a follow-up.
- **Lower yield on PR-driven repos.** Pallets/Werkzeug/Flask/httpx all merge-commit their PRs; `commit_runtime` correctly rejects those merge commits and yields ~0 directly mineable bugfix commits. Use `pr_runtime` for those repos — that's its turf.
- **Go-heavy reference dataset.** 39/52 envs are Go (urfave/gin/mux/cobra/testify/logrus) because Go projects in the bootstrap-cached set commit directly + squash-merge more than the Python projects do. This isn't a `commit_runtime` defect — it's the pipeline's natural fit shining through. Documented up-front for any eval that wants language balance.
- **No `commit_stream` continuous variant.** Same call as Arc 2: if continuous mining ever becomes a hard requirement, it's flags on `commit_runtime` (`since=auto` + `state-file=…`), not a separate pipeline.

## How it was generated

```bash
# Per repo: bootstrap (cached from Arc 2) + mine/validate commits
repo2rlenv generate --repo <owner>/<repo> --pipeline commit_runtime \
  --pipeline-opt limit=120 --pipeline-opt clone_depth=400 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./stage/<repo>

# Oracle-gate (keep reward 1.0), drop broken oracles, push
harbor run -p ./pool -a oracle --env docker -n 5
repo2rlenv push ./dataset AdithyaSK/repo2rlenv-commit-runtime --inline-dockerfile
```

Scale driver: `plans/arc3_scale.py` (gitignored, launch-side).
