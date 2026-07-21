# RFC 0002: `pr_runtime`

**Status:** implemented
**Author:** `@adithya-s-k`
**Created:** 2026-02-01 *(retrospective — pipeline shipped in v0.3.0; RFC written 2026-07-15 as archival record)*

## Summary

SWE-bench-style PR mining with **sandbox-verified F2P/P2P test oracles**. Mines merged PRs and runs the repo's actual test suite inside a Docker container built by the bootstrap phase; only PRs whose new tests flip fail→pass under the gold patch (while all pre-existing tests stay green) are emitted. Graded reward: `reward = f2p_rate × p2p_rate`. The flagship of the runtime family.

## Motivation

`pr_diff` is cheap but its reward is *proxy* — diff similarity to the merged patch, not an execution signal. For an RL loop that trains agents to actually make code work, you want the same signal SWE-bench uses: **did the patch fix the failing test, and did no other test break?** That's F2P/P2P. It requires a working sandbox for the target repo, which is where our `bootstrap/` phase comes in. `pr_runtime` is the pipeline that consumes bootstrap and turns each candidate PR into a runnable Harbor task.

The counter-argument at the time: "sandbox setup is a lot of engineering; SWE-bench already exists." True but SWE-bench is a fixed 2K-task benchmark, not a pipeline that works on arbitrary repos. Repo2RLEnv's contribution is the **reusable pipeline** — point it at any repo with a working test suite and get F2P/P2P-graded tasks. Every non-Django repo in our datasets is proof of that.

## Design

### Input

- **Source** — GitHub · GitLab (input-source abstraction from v0.8.4).
- **Trigger** — `repo2rlenv generate --pipeline pr_runtime --repo <owner>/<name> --pipeline-opt limit=60 --llm anthropic/claude-sonnet-4-6 ...`
- **Options model** — `PrRuntimeOptions`: mining knobs (`limit`, `since`, `until`, `skip_drafts`) + structural filters (`require_new_test_funcs`, `min_problem_statement_words`, `lite_filter`, `max_files_per_pr`) + validation (`require_fail_to_pass`, `min_fail_to_pass`, `validation_timeout_sec`).

### Algorithm

1. `gh pr list --state merged` → candidate PRs.
2. `_bootstrap` — LLM agent iterates shell commands in a fresh Docker container until the repo builds and its test suite collects. Result is cached content-addressed under `envs/<repo>__<sha>/`.
3. Per PR: fetch metadata (title, body, `base.sha` via REST — #73), split diff into `(source_patch, test_patch)`.
4. **Two-stage validation inside the bootstrap sandbox**:
   - Pre-fix: apply only the test patch on top of `base_commit`, run tests → those failing are candidate F2P.
   - Post-fix: apply the full merged diff, run tests → all F2P must pass, all P2P must stay green.
5. **Instruction sourcing** — when the PR has `Closes #N`, fetch the linked issue body (leak-free); otherwise fall back to the PR body run through `_strip_info_leak` + `_reflow_pr_body`.
6. Emit a Harbor task with the shared shape.

### Output

- Task shape: `environment/Dockerfile` (built on the bootstrap image), `environment/docker-compose.yaml` (egress guard), `tests/{test.sh, verifier.py, f2p.json, p2p.json}`, `solution/{patch.diff, solve.sh}`, `instruction.md`, `task.toml`.
- `[metadata.repo2env]` provenance: standard fields plus `pr_runtime` subtable (`pr_url`, `pr_merged_at`, `base_commit`, `fail_to_pass`, `pass_to_pass`) and `reward_calibration` (F2P/P2P counts, LOC, difficulty).

## Verification

- **Reward kinds** — `test_execution` + `diff_similarity`.
- **Reward formula** — graded: `reward = f2p_rate × p2p_rate`.
- **Two eval signals** (Arc 2 split): **`resolved`** = SWE-bench-tracked (all F2P + P2P pass); **`command_resolved`** = stricter (resolved AND no untracked failures AND `exit_code==0`). Plus **`eval_grade`** = `command_resolved` AND `p2p_count > 0`. Documented in [`docs/reference/REWARD_SCHEMA.md`](../reference/REWARD_SCHEMA.md).
- **Oracle invariant** — gold patch scores `reward=1.0, resolved=True` on every task. Enforced by dropping tasks that fail the invariant during the oracle gate.
- **Non-tamper** — `test.sh` resets test files to `base_commit` and re-applies the heredoc'd test-patch at every invocation, so an agent that edits/deletes tests loses.

## Anti-contamination

Full v0.8.5 defenses baked in:

- **Git-history scrub** — remove `origin`, prune future refs, `gc`.
- **Egress guard** — compose overlay blackholing `pypi.org`, `github.com`, and CDNs.
- **Instruction leak-strip** — issue-fetch fallback (bug reports don't name the fix); `_LEAK_PATTERNS` scrub SHAs, fix-PR links, `Closes #N`, `(#NNNN)`, `repo#N`.

## LLM use

- **`at bootstrap` (cached)** — one call chain per (repo, ref) for the LLM agent that iterates the Docker env into a build-and-test-green state. Amortized across all tasks from that repo. Cost per repo: ~$3–8 uncached, $0 cached.
- **Zero per-task LLM cost** during mining (the instruction is real issue/PR text, not synthesized).

## Yield & repo suitability

- **15–40% yield.** Dominant factor: does the PR ship a new test that flips fail→pass, and does the suite run green in the container?
- **What works**: pytest-clean Python (pallets, requests, httpx, attrs, werkzeug, flask, typer, aiohttp), Go with `go test` (urfave/cli, gin, mux, cobra, testify, logrus), Node with jest, Rust with cargo test.
- **What doesn't**: ML repos whose tests need CUDA, tests that need network, monorepos where tests span multiple packages.

## Dependencies

- **`bootstrap/`** — LLM-driven Docker env build + content-addressed cache.
- **`_pr_runtime_verifier.py`** — the in-container graded scorer. Shared with `commit_runtime`, `cve_patches`.
- **`_env_guard.py`** — anti-contamination.
- Standard tooling: `gh` CLI + REST for PR fetch, LiteLLM for bootstrap.

## Alternatives considered

- **Binary exit-code reward** — shipped that way originally in v0.3.0. Upgraded to graded F2P/P2P in v0.8.3 (Arc 2) because the binary form gave the same 0.0 to an agent that fixed 4/5 tests as to one that fixed 0.
- **Whole-suite P2P** — considered; too flaky. Went with the *tracked* P2P set (tests known to pass at HEAD before the fix), which is what SWE-bench does.

## Rollout plan

Historic. v0.3.0 shipped the binary version; v0.8.3 (Arc 2) shipped the graded version + 100-env reference dataset. Ongoing improvements: PEP 735 `--group tests` recipe fix, `command_resolved` split (audit rev 2), enriched manifest with checksums.

## Open questions

Historic — none active. The `command_resolved` / `eval_grade` split is now the stable model.

## References

- SWE-bench: [arXiv:2310.06770](https://arxiv.org/abs/2310.06770), [SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench).
- SWE-Gym: [arXiv:2412.21139](https://arxiv.org/abs/2412.21139).
- Full v0.8.3 audit: [`docs/release_notes/v0.8.3/findings-pr_runtime.md`](../release_notes/v0.8.3/findings-pr_runtime.md).

## Implementation

| | |
|---|---|
| **Initial PR** | [#4](https://github.com/huggingface/Repo2RLEnv/pull/4) — `pr_runtime` pipeline v0.3: SWE-bench-style PR mining with sandbox verification |
| **Shipping release** | v0.3.0 |
| **Source file** | [`src/repo2rlenv/pipelines/pr_runtime.py`](../../src/repo2rlenv/pipelines/pr_runtime.py) |
| **Verifier** | [`src/repo2rlenv/pipelines/_pr_runtime_verifier.py`](../../src/repo2rlenv/pipelines/_pr_runtime_verifier.py) |
| **Validation harness** | [`src/repo2rlenv/pipelines/pr_runtime_validate.py`](../../src/repo2rlenv/pipelines/pr_runtime_validate.py) |
| **Options model** | [`src/repo2rlenv/spec/options.py`](../../src/repo2rlenv/spec/options.py) — `PrRuntimeOptions` |
| **Doc page** | [`docs/pipelines/pr_runtime.md`](../pipelines/pr_runtime.md) |
| **Findings / release notes** | [`docs/release_notes/v0.8.3/findings-pr_runtime.md`](../release_notes/v0.8.3/findings-pr_runtime.md) |
| **Reference dataset** | [`AdithyaSK/repo2rlenv-pr-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime) (100 oracle-verified envs; 100 tracked / 88 command / 87 eval_grade) |
| **Follow-up PRs** | [#45](https://github.com/huggingface/Repo2RLEnv/pull/45) Arc 2 (graded F2P/P2P + tracked/command_resolved split + enriched manifest) · [#63](https://github.com/huggingface/Repo2RLEnv/pull/63) GitLab source · [#69](https://github.com/huggingface/Repo2RLEnv/pull/69) anti-contamination defenses · [#73](https://github.com/huggingface/Repo2RLEnv/pull/73) fetch base_sha via REST · [#75](https://github.com/huggingface/Repo2RLEnv/pull/75) Harbor spec: reward-details.json sidecar |
