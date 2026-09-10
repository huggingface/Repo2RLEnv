# SWE-gen reproduction pilot

Original SWE-gen processed its documented axios/axios#7150 HTTP/2 example. Native validation and a separate byte-preserving Harbor export both produce no-op 0 and reference 1. The authored verifier runs 22 HTTP/2 tests; the native task removes git history and uses no-network mode. Blind rollouts and adversarial audits are pending.

| Measurement | Result |
| --- | --- |
| Native emitted | 1 |
| Native accepted | 1 |
| Harbor task definitions | 1 |
| Reference/no-op contrast passes | 1 |
| Native/Harbor parity | 1 |
| Training approved | 0 |

Models: gpt-5.5 instruction; claude-opus-4-8 author.

## Recorded adaptations and scope

- One documented PR, native prompts and default model selection
- Author capped at 80 turns, $8 and 1500 seconds
- IS_SANDBOX=1 for the original Claude SDK inside the remote VM

Source pin: `14e185f413f7bff03f8f9fec6fb246681bf61d74`. Commands are exposed by `python reproductions/run.py plan swe-gen`.

Evidence: `swe-gen/export-audit.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/swe-gen/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
