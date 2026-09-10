# SCALER reproduction pilot

Executed the released family expander for 1003_F. Abbreviation at difficulties 4, 8 and 12 in SandboxFusion. The original reference program computed answers 5, 45 and 111. All three Harbor instances reproduce the native -1/+1 reward scale. These are algorithmic reasoning instances, not repository coding environments, and no new family was synthesized.

| Measurement | Result |
| --- | --- |
| Native emitted | 3 |
| Native accepted | 3 |
| Harbor task definitions | 3 |
| Reference/no-op contrast passes | 3 |
| Native/Harbor parity | 3 |
| Training approved | 0 |

Models: no LLM calls for released-family expansion.

## Recorded adaptations and scope

- Released family expansion only: the source family-synthesis execution loop is commented out
- Original generator and reference programs executed in SandboxFusion
- Unchanged native reward function; fixed-instance reference-answer replay supplies the Harbor oracle

Source pin: `60c6c5037866c718f4c001ea338f9c5a91cb01ae`. Commands are exposed by `python reproductions/run.py plan scaler`.

Evidence: `scaler/export-audit.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/scaler/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
