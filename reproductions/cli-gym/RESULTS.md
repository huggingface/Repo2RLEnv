# CLI-Gym reproduction pilot

One native environment-inversion attempt emitted two instruction variants sharing the same environment. Both the native parser and Harbor award reward 1 to the unchanged environment after a malformed test runner produces no parsed results. No restoration oracle was emitted. These are rejected for training.

| Measurement | Result |
| --- | --- |
| Native emitted | 2 |
| Native accepted | 2 |
| Harbor task definitions | 2 |
| Reference/no-op contrast passes | 0 |
| Native/Harbor no-op reward agreement only | 2 |
| Training approved | 0 |

Models: gpt-4o.

## Recorded adaptations and scope

- GPT-4o replaces upstream Qwen default; original prompts and agent loop retained
- Bounded to 30 inversion episodes and 4096 output tokens
- Selected configuration-file damage after preserving a proposal for 50 parallel 1GB writes without executing it
- Native pytest parser and empty-result behavior preserved; removed learner git metadata and disabled internet

Source pin: `48bb920b728a25a55a5b442303e901919654599e`. Commands are exposed by `python reproductions/run.py plan cli-gym`.

Evidence: `cli-gym/export-audit.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/cli-gym/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
