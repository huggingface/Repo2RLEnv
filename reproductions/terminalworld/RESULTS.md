# TerminalWorld (EuniAI) reproduction pilot

Retrieved three documented recording IDs; only 100135 passed the initial PII screen. Native scoring accepted it as silver (8), then the original workflow generated instructions, environment, tests and a refined task. Native reference=1, no-op=0, and all three partial solutions=0. A separate Harbor export rebuilt from its final Docker context also passes reference/no-op contrast. Internet remains enabled in the native task; independent blind and adversarial audits are pending.

| Measurement | Result |
| --- | --- |
| Native emitted | 1 |
| Native accepted | 1 |
| Harbor task definitions | 1 |
| Reference/no-op contrast passes | 1 |
| Native/Harbor parity | 1 |
| Training approved | 0 |

Models: native Haiku and Sonnet helpers; claude-sonnet-4-6 author/refiner.

## Recorded adaptations and scope

- Restored commands_with_output omitted by the upstream stage serializer and normalized extracted command strings; original scorer prompts/thresholds unchanged
- Preserved released skills under paths expected by the native agent launcher; IS_SANDBOX=1 in remote VM
- Restored exact Docker COPY asset moved out of the build context by native cleanup
- Native builder adapted an unavailable historical data URL using another NASA URL already present in metadata
- Original refiner reached its 40-turn cap after reference/no-op success; native --repair completed under a 60-turn/$3 cap
- Recorded agent-side Harbor Trial API compatibility fix; final export rebuilt before validation

Source pin: `784698ba93735470ce1664bff2ec44bcd7b28e15`. Commands are exposed by `python reproductions/run.py plan terminalworld`.

Evidence: `terminalworld/export-audit.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/terminalworld/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
