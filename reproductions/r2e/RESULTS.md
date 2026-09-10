# R2E reproduction pilot

Native extraction found 18 functions and 53 methods in python-graphs. One ast_height function received seven generated tests and passed the native execution check with reported 100% line and branch coverage. Harbor no-op 0 and reference 1 also pass. The reference helper exposed during grading needs adversarial review before training use.

| Measurement | Result |
| --- | --- |
| Native emitted | 1 |
| Native accepted | 1 |
| Harbor task definitions | 1 |
| Reference/no-op contrast passes | 1 |
| Native/Harbor parity | Not established |
| Training approved | 0 |

Models: gpt-4o.

## Recorded adaptations and scope

- Original Docker builder failed on removed Ubuntu packages; used the supported --local mode inside the remote Modal VM
- Downloaded missing NLTK punkt_tab data and retained native extraction/slicing/test generation
- One selected function, up to three generation rounds, 1024-token cap; GPT-4o model substitution
- Minimal function-body removal and docstring instruction; original tests unchanged; source reference replaces native compiled reference service

Source pin: `bcbed156711bb939de14aa46b27eee15073f5272`. Commands are exposed by `python reproductions/run.py plan r2e`.

Evidence: `r2e/export-audit.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/r2e/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
