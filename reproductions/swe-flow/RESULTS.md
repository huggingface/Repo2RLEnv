# SWE-Flow reproduction pilot

Original SWE-Flow-Trace selected five tests in spectree and the native synthesis pipeline emitted four reconstruction tasks. Three pass native and Harbor reference/no-op contrast. The fourth fails in both: its reference patch joins two statements on one line, causing a SyntaxError in starlette_plugin.py. Conversion parity includes agreement on this failure.

| Measurement | Result |
| --- | --- |
| Native emitted | 4 |
| Native accepted | Not established |
| Harbor task definitions | 4 |
| Reference/no-op contrast passes | 3 |
| Native/Harbor parity | 4 |
| Training approved | 0 |

Models: gpt-4o.

## Recorded adaptations and scope

- One published spectree runtime, five tests, seed 42, two trace workers
- GPT-4o replaces native model defaults for 31 docstrings and four specifications
- Preserved patch bytes and used git apply --inaccurate-eof -p0 for native EOF metadata
- Harbor packaging removes installed healthy package, backup source and git history; hidden tests copied only for verification

Source pin: `7da5b046fa1dc184674e4e94a9989be56c39e4e7`. Commands are exposed by `python reproductions/run.py plan swe-flow`.

Evidence: `swe-flow/export-audit-v3.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/swe-flow/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
