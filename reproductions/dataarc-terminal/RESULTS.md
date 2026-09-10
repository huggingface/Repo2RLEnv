# DataArc terminal synthesis (Envs-FORGE-linked code) reproduction pilot

The released toolkit accepted one cancel-async-tasks variant through static file checks. Harbor no-op and reference both score 0. Five of six reference tests fail: test.py imports run_tasks, while the authored reference does not export that function; cancellation tests also observe no required start logs. The verifier installs dependencies at execution time and requires network access.

| Measurement | Result |
| --- | --- |
| Native emitted | 1 |
| Native accepted | 1 |
| Harbor task definitions | 1 |
| Reference/no-op contrast passes | 0 |
| Native/Harbor parity | Not established |
| Training approved | 0 |

Models: gpt-4o.

## Recorded adaptations and scope

- One released few-shot seed and original toolkit generator
- GPT-4o replaces the upstream DeepSeek default
- Byte-preserving Harbor contract export; failing oracle and tests retained

Source pin: `2a1d65ec8dcfaea2458d67e1fb18078cce6420b9`. Commands are exposed by `python reproductions/run.py plan dataarc-terminal`.

Evidence: `dataarc-terminal/audit-summary.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/dataarc-terminal/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
