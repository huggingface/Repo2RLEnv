# SWE-Next reproduction pilot

Ran the original PR pipeline on Black and Pyramid. It emitted three dataset rows despite empty FAIL_TO_PASS and PASS_TO_PASS sets; neither repository produced an accepted contrast-positive task. Black fails test collection on optional-tests configuration. The first Pyramid cap excluded all eligible changes; increasing the input cap exposed two candidates but did not produce positive execution contrast. No rows were exported to Harbor.

| Measurement | Result |
| --- | --- |
| Native emitted | 3 |
| Native accepted | 0 |
| Harbor task definitions | 0 |
| Reference/no-op contrast passes | 0 |
| Native/Harbor parity | Not established |
| Training approved | 0 |

Models: native claude-sonnet-4-5 environment profiles; no successful issue generation.

## Recorded adaptations and scope

- At most 30 PR records per repository, one worker, one quarter-environment profile
- Native image publishing and solver trajectories disabled for the pilot
- Recorded wrapper/environment compatibility and response-only telemetry
- Bounded pre-filter input cap expanded from 3 to 30 for Pyramid; original eligibility rules unchanged

Source pin: `b55c0841f364f9fe7363b2012cd0ae8d8afdf872`. Commands are exposed by `python reproductions/run.py plan swe-next`.

Evidence: `swe-next/Pylons-pyramid-generation-summary.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/swe-next/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
