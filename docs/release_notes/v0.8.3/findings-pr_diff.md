# `pr_diff` — instruction info-leak hardening

The v0.8.1 implementation stripped `Closes / Fixes / Resolves #N` from PR
bodies. A local sweep across 38 repos (Tier A SWE-bench + Tier B HF
ecosystem + Tier C Go/Rust/Node/TS) surfaced several patterns that still
leaked the answer:

1. **Multi-issue closes**: `Fixes #1, #2, #3` — only the first `#N` was stripped.
2. **`See` / `refs` / `follow-up to` linkbacks**: pointers to related issues.
3. **Markdown issue links**: `[#1234](https://github.com/x/y/issues/1234)`.
4. **Closes with markdown-link refs**: `Closes [#1234](url)` — the bare-`#N`
   strip didn't catch the markdown-link form, leaving `Closes ` orphaned.
5. **Descriptive markdown links to GH URLs**:
   `[my analysis](https://github.com/x/y/pull/1234)` — the bare-URL strip
   left `[my analysis]()` brackets behind.
6. **Bare GitHub URLs** to `/pull/`, `/issues/`, `/commit/` — including
   `https://redirect.github.com/...` from Dependabot release notes.
7. **Trailer lines**: `Co-authored-by`, `Signed-off-by`, `Reviewed-by`, `Acked-by`.
8. **Title squash suffix**: GitHub's `" (#1234)"` AND manual
   `" (fixes #1800)"` patterns on the PR title itself.

All six patterns are now stripped before instructions land in
`task.toml.instruction`. Implementation in
[`src/repo2rlenv/pipelines/pr_diff.py`](../../../src/repo2rlenv/pipelines/pr_diff.py):
`_strip_info_leak`, `_build_instruction`, plus 5 compiled regexes.

## Verification

After re-running the sweep with the hardened strip, **zero of the 127
emitted instructions** match any leak pattern:

```bash
# Replace <dir> with your local pr_diff output directory
find <dir> -name instruction.md \
  -exec grep -lEi "(closes|fixes|resolves)\s+#[0-9]+" {} \;
# (empty)

find <dir> -name instruction.md \
  -exec grep -lEi "https?://([a-z0-9.-]+\.)?github\.com/.*/(pull|issues|commit)/" {} \;
# (empty)

find <dir> -name instruction.md \
  -exec grep -lEi "^(Co-authored-by|Signed-off-by|Reviewed-by|Acked-by):" {} \;
# (empty)
```

Before the fix, the same 38-repo run produced 1 title-leak
(`stretchr/testify`) and 6 dependabot-style body leaks (`gin`,
`jsonschema`, `urfave/cli`, `expressjs`, `chronotope`).

## Tests

18 new unit tests in
[`tests/test_pipeline_pr_diff.py`](../../../tests/test_pipeline_pr_diff.py)
cover each pattern + the end-to-end `_build_instruction` shape. Total
suite: **638 passing** (+18 net from this PR).
