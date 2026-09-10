# SecVerifier reproduction pilot

Installed the original OpenHands-based multi-agent implementation. Fixed a credential SecretStr mismatch in our wrapper. The next attempt failed before inference because the SEC-bench image lacks the expected OpenHands micromamba runtime. Builder/Exploiter/Fixer execution and Harbor output remain pending.

| Measurement | Result |
| --- | --- |
| Native emitted | 0 |
| Native accepted | 0 |
| Harbor task definitions | 0 |
| Reference/no-op contrast passes | 0 |
| Native/Harbor parity | Not established |
| Training approved | 0 |

Models: gpt-4o configured; no inference reached.

## Recorded adaptations and scope

- Poetry lock retained; CC=gcc and CXX=g++ for a dependency build
- Added undeclared pandas/datasets dependencies
- User credential bound as SecretStr only in memory
- Configured original CLI for one task, 30 iterations, $5 model limit, 4096 output tokens

Source pin: `93abf5900327809eacff66fec40d45eb95221acb`. Commands are exposed by `python reproductions/run.py plan secverifier`.

Evidence: `secverifier/native-output` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/secverifier/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
