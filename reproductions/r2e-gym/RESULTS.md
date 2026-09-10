# R2E-Gym / SWEGEN reproduction pilot

Native mining over a bounded Pyramid clone parsed 369 commit records and selected one candidate after its eligibility filters. The original test extractor copies tutorial test files into one package; both revisions fail collection because the tutorial module context is missing (825 tests collected, five collection errors). Native outcome NEW_COMMIT_NOT_BETTER; no generated environment or issue.

| Measurement | Result |
| --- | --- |
| Native emitted | 0 |
| Native accepted | 0 |
| Harbor task definitions | 0 |
| Reference/no-op contrast passes | 0 |
| Native/Harbor parity | Not established |
| Training approved | 0 |

Models: no issue-generation inference reached.

## Recorded adaptations and scope

- Added the pinned R2E dependency missing from the declared runtime
- Single mining worker instead of hardcoded 32; shallow clone depth 100 includes merged ancestors
- Expanded the pre-filter record cap to include the full bounded clone
- Supported local execution inside the remote Modal VM; no Mac execution

Source pin: `0d94c4eb9431cd195c55a7ea3abd54006c9a1735`. Commands are exposed by `python reproductions/run.py plan r2e-gym`.

Evidence: `r2e-gym/generation-summary.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/r2e-gym/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
