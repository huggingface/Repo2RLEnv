# SEC-bench reproduction pilot

Original builder constructed njs.cve-2022-32414 from the released SEC-bench/Seed record. SecVerifier subsequently failed to start because the published secb.base image lacks /openhands/micromamba/bin/micromamba. This is an image/runtime contract failure before agent inference; construction alone is not a generated verified task.

| Measurement | Result |
| --- | --- |
| Native emitted | 1 |
| Native accepted | 0 |
| Harbor task definitions | 0 |
| Reference/no-op contrast passes | 0 |
| Native/Harbor parity | Not established |
| Training approved | 0 |

Models: no model used for image construction.

## Recorded adaptations and scope

- One released processed CVE seed, not fresh vulnerability mining
- Pinned source, captured dataset revision and image inspect receipt
- Builder-only dependencies isolated from the separately pinned SecVerifier runtime

Source pin: `31eb43485a3de47da260be0f978528b1f2314415`. Commands are exposed by `python reproductions/run.py plan sec-bench`.

Evidence: `sec-bench/native-image.json` in the phase-two snapshot; stage logs and receipt hashes under `runs/runner/reproduction-worker-02/sec-bench/`. See [overall status](../STATUS.md) for the current campaign state. Small pilot results do not establish the 100-input target.
