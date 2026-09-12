# R2E-Gym / SWEGEN adaptation

Source: [R2E-Gym / SWEGEN](https://github.com/R2E-Gym/R2E-Gym), Apache-2.0, commit
`0d94c4eb9431cd195c55a7ea3abd54006c9a1735`. `UPSTREAM_LICENSE` retains the license.
`instruction_prompt.md` retains `ISSUE_INSTRUCTIONS` from
`src/r2egym/repo_analysis/build_syn_issue.py`; `issue_examples.json` retains the seven
examples from `repo_analysis/issues/combined_issue.py`. The caller selects the model.

Workflow: First-parent commit history → bounded bug edits and matching test changes → extracted post-change test files under r2e_tests → old/new execution → grounded issue → Harbor rebuild.

The native strict test identity comparison is retained. Test files are renamed under r2e_tests. Repository-specific Pillow, NumPy, Datalad and Tornado import/runner heuristics are not supported in the initial ordinary-pytest profile. The native optional bug-edit/test-match switches default on, as in the published generation guide.

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
