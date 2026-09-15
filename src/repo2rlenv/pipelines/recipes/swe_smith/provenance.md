# SWE-smith adaptation

Source: [SWE-bench/SWE-smith](https://github.com/SWE-bench/SWE-smith), commit
`9b74ac08118a85c39c356802f7961893af73e07f`. Copyright 2025 John Yang; MIT.
The adjacent `UPSTREAM_LICENSE` preserves the upstream notice.

This implementation follows procedural defect generation in
`swesmith/bug_gen/procedural/python/{operations,control_flow}.py`, execution
validation in `swesmith/harness/valid.py`, and issue writing from failing tests in
`swesmith/issue_gen/get_from_tests.py` and `configs/issue_gen/ig_tests.yaml`.

Repo2RLEnv owns the implementation. It enumerates seeded single-site LibCST
mutations instead of sampling upstream multi-edit probabilities; uses explicit
repository profiles and its existing bootstrap cache instead of the upstream
profile registry; checks complete JUnit identities; and emits Harbor tasks with
a separate verifier. Issue generation preserves the evidence-to-bug-report
method and avoids requiring SWE-bench demonstrations. This version does not
implement SWE-smith's LLM rewrite or mutation-combination strategies.

No upstream research package, profile cache or demonstration dataset is required
at runtime. Repository source retains its own license in exported snapshots.
