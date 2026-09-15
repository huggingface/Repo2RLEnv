# SWE-Next adaptation

Source: [SWE-Next](https://github.com/TIGER-AI-Lab/SWE-Next), Apache-2.0, commit
`b55c0841f364f9fe7363b2012cd0ae8d8afdf872`. `UPSTREAM_LICENSE` retains the license.
`instruction_prompt.md` retains `ISSUE_INSTRUCTIONS` from
`src/swenext/repo_analysis/build_syn_issue.py`; `issue_examples.json` retains the seven
examples from `repo_analysis/issues/combined_issue.py`. The caller selects the model.

Workflow: Merged PR metadata → merge commit and first parent → bounded Python/test changes → original-path post-change tests → old/new execution → grounded issue → Harbor rebuild.

The default original-path test layout is retained. Quarterly LLM environment profiles are replaced by an explicit dependency profile and the existing content-addressed bootstrap cache. The native intersection/file-level comparison fallback is deliberately replaced by exact nonempty test identity equality for this generation profile.

Shared owned code in `recipes/history/` implements snapshot selection, AST entity
comparison, remote execution and issue assembly. Entity comparison uses Python AST
without comments/docstrings; statement edit counts use structural set differences
rather than the upstream diff-entity implementation. This is a recorded heuristic
adaptation. Native size bounds and recipe-specific bug/test policies remain explicit.

Only modified existing Python source files and added/modified Python test files
are supported. Source roots must cover all changed Python implementation files.
Non-Python changes are not part of the repair; the standalone environment starts
from the old source snapshot, preserving legitimate old repository context.
Post-change tests are private. Dependencies are built at the new revision for
contrast extraction; the standalone old snapshot is independently rebuilt and
checked with both baseline and reference before export. Incompatible builds fail
with evidence. The reference must pass the selected test files completely.

The owned model response uses structured private analysis and a public instruction
instead of bracket tags. Test evidence, patches and execution logs are generation
context; the reference remains private. No research repository is installed,
imported or cloned at runtime. Original target repositories are fetched only in
remote workers. Training, solver trajectories and independent quality acceptance
are deferred; these are generated artifacts, not approved training examples.
