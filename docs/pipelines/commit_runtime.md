# `commit_runtime`

R2E-Gym SWE-GEN-style PR mining: walk **commits**, not PRs. Trades signal quality for yield, and catches drive-by fixes that never went through a PR — repos that squash-merge or commit directly to main aren't reachable from `pr_runtime` at all.

**As of v0.8.3 the instruction is sourced the same leak-resistant way `pr_runtime` does** (Arc 3 findings: [`findings-commit_runtime`](../release_notes/v0.8.3/findings-commit_runtime.md)). When a commit links an issue with `Closes #N`, the GitHub issue body becomes the problem statement (less leak-prone than the commit message, which often names the function being fixed). Otherwise the commit subject + body are leak-stripped and reflowed.

**Reference dataset**: [`AdithyaSK/repo2rlenv-commit-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime) — 52 oracle-verified envs across 12 repos (Python + Go).

| | |
|---|---|
| Status | **experimental** (will promote once the no-linked-issue tail is improved) |
| Sandbox at generation | Yes — reuses the bootstrap image from `pr_runtime` |
| LLM use | bootstrap-time only (one-time env build, cached). **No** per-task synthesis — the prompt is the real issue or commit message, not generated. |
| Reward | Graded F2P/P2P (`reward = f2p_rate × p2p_rate`) via the in-container verifier, identical to `pr_runtime`. Tracked / `command_resolved` / `eval_grade` split documented in [`pr_runtime`](./pr_runtime.md). |
| Languages | Any (commit_runtime inherits language-agnostic bootstrap from `pr_runtime`) |
| Inspired by | [R2E-Gym (SWE-GEN)](https://github.com/R2E-Gym/R2E-Gym) (Jain et al., COLM '25) |

## Why commits, not PRs

R2E-Gym's headline finding: *"instead of using human-written PRs, good-quality execution environments can directly be curated from commits."* Commit-based curation:

- **No PR-review bottleneck.** Works on any repo with commit history, including ones that never use PRs (research / internal / solo-maintained).
- **Larger candidate pool.** 3-10× bigger than the PR list for most repos.
- **Noisier signal.** No reviewer signed off; filters have to do all the work.

`commit_runtime` is a **sibling** of `pr_runtime`, not a replacement. Each works best on different repo shapes:

| Repo style | Better fit |
|---|---|
| Squash-merge + PR-first (pallets, Django, etc.) | `pr_runtime` — every fix is a PR; commits look like a wall of merge commits |
| Direct-commit + squash-merge (Go projects, single-maintainer crates, internal repos) | `commit_runtime` — fixes land as plain commits, not behind PR merges |

Arc 3's 52-env dataset is **Go-heavy** for exactly this reason: `urfave/cli` (15), `gin` (8), `mux` (6) are commit-friendly; `pallets/click` (7) is the Python exception with enough non-PR commits to mine.

## Algorithm

1. `git clone --depth N` (default `clone_depth=200`; bump for monthly mining).
2. `git log --first-parent <since>..<until>` ⇒ candidate commit list.
3. **Metadata filter** (`_metadata_filter`):
   - Drop merge commits (`skip_merge_commits`)
   - Drop excluded authors (bots)
   - Drop short messages (< `min_message_words`, default 5)
   - **Reject non-bugfix conventional-commit types** (`chore:` / `docs:` / `feat:` / `refactor:` / `style:` / `test:` / `ci:` / `build:` / `perf:` / `revert:`)
   - **Require a bugfix-positive signal**: `fix:` prefix OR `Closes #N` issue trailer OR a bugfix keyword in the subject (`fix` / `bug` / `regression` / `crash` / `broken` / `incorrect` / `wrong` / `fail` / …)
4. `git show <sha>` ⇒ split into `(source_patch, test_patch)` using `_split_patch_and_test_patch` from `pr_runtime`.
5. **Structural filter** (`_structural_filter`): skip CI-only diffs, sweeping refactors (file count > `max_source_files_per_commit`), and commits without ≥1 new test function (`require_new_test_funcs`).
6. **Validate inside the bootstrap sandbox** (`pr_runtime`'s `validate_pr` harness): pre-fix run discovers `FAIL_TO_PASS` + `PASS_TO_PASS` sets; post-fix run confirms the flip.
7. **Build instruction** (`build_instruction_from_commit`):
   - If `Closes #N` is present ⇒ fetch the GitHub issue body via `github.fetch_issue` and use it as the problem statement.
   - Otherwise ⇒ commit subject + body, run through `_strip_info_leak` + `_reflow_pr_body` to remove cross-refs (SHAs, fix-PR links, `(#NNNN)` squash trailers, `repo#N` cross-repo refs) and trim template noise.
8. Emit a Harbor task with the same shape as `pr_runtime`: `environment/Dockerfile`, `tests/test.sh`, `tests/verifier.py`, `tests/f2p.json`, `tests/p2p.json`, `solution/patch.diff`, and the full `[metadata.repo2env]` block including `reward_calibration` (`f2p_count`, `p2p_count`, `source_files`, `loc_changed`, `difficulty`).

## Options (`CommitRuntimeOptions`)

```python
limit: int = 50                    # max candidate commits to walk
since: date | None = None
until: date | None = None
branch: str = "HEAD"
clone_depth: int = 200             # bump for monthly mining

# Metadata filters (cheap, applied before validation)
skip_merge_commits: bool = True
min_message_words: int = 5
max_source_files_per_commit: int = 10
exclude_authors: list[str] = []    # e.g. ["dependabot[bot]@users.noreply.github.com"]
require_new_test_funcs: bool = True
skip_ci_only: bool = True

# Validation
require_fail_to_pass: bool = True
min_fail_to_pass: int = 1
validation_timeout_sec: int = 600
```

## Known limitations (Arc 3 audit)

- **Thin instructions when no issue link.** ~30% of emitted tasks lacked a `Closes #N`, so the instruction is sourced from the commit subject + body — which can be a one-liner. `pr_runtime` is naturally better when there's an issue to fall back on.
- **Lower yield on PR-driven repos.** Pallets/Django/Flask-style repos merge-commit their PRs; `commit_runtime` correctly rejects all those merge commits and so yields ~0 there. Use `pr_runtime` for those.
- **Go-subtest parser over-counts untracked failures.** A handful of `stretchr/testify` tasks land tracked-resolved but **not** command_resolved because the verifier marks Go subtests as "untracked failed" when the parent test exited 0. Affects `command_resolved`/`eval_grade` only; gold-patch reward is still 1.0.
- **No `commit_stream` / continuous variant.** `pr_stream` was removed in v0.8.3 as scope-creep; no plans to add a commit equivalent. If needed in future, it would be flags (`since=auto` + `state-file=…`) on `commit_runtime`, not a separate pipeline.

See [`findings-commit_runtime`](../release_notes/v0.8.3/findings-commit_runtime.md) for the full Arc 3 changelog + audit.
