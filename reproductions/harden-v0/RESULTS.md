# harden-v0 reproduction pilot

Ran one original hacker/fixer iteration on the Endless requests-installation task. Oracle and hacker both scored 1; the fixer classified the attempt as legitimate because it really created the venv and installed requests. It changed no tests and attempted no replay. The independently demonstrated counterfeit-Python shortcut remains unresolved; this run supplies no evidence that the task is hardened.

| Measurement | Result |
| --- | --- |
| Native emitted | 0 |
| Native accepted | Not established |
| Harbor task definitions | 0 |
| Reference/no-op contrast passes | 0 |
| Native/Harbor parity | Not established |
| Training approved | 0 |

Models: claude-sonnet-4-6.

## Recorded adaptations and scope

- One original iteration, capped hacker/fixer turns, native oracle precheck and replay setting retained
- Added tmux to the runtime used by the original terminal agent

Source pin: `342b8474e0c0cf96e4a8313fd2e26c7a11d51193`. Commands are exposed by `python reproductions/run.py plan harden-v0`.

Evidence: `harden-v0/native-output/task_000000_48ab827c/result.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/harden-v0/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
