# TMax reproduction

The original legacy generator emitted **one task from one input** on September
10, 2026; its first definition round passed build and initial tests. The task
covers a C matrix library, Python bindings, gRPC integration and tests.

A metadata serialization patch fixes the released converter's crash on
`base_image: null`. Instruction, setup and final-test semantics are preserved.
The original generator does not emit a reference solution.

| Validation stage | Result |
| --- | --- |
| Native untouched task | 33 failures / 18 errors; verifier completed |
| First native Sonnet sample, 20 actions | 48/51 tests; missing integration-test file |
| Fresh 40-action sample | Interrupted when a text-only reply triggered invalid assistant-prefill retries |
| Replay of that sample's 20 actual commands | 50/51 tests; missing pytest-timeout plugin |
| Separate image with pytest-timeout==2.4.0 | Same replay passes all 51 native tests |
| Corrected image through fresh Harbor | No-op 0 / replay 1, no exception |
| Fresh provider-compatible original solver | Native success, 26 response receipts |

The verifier invokes `pytest --timeout=120`, but the generated image omits the
plugin providing that option. A separate runtime variant adds it to both native
and Docker images. **Instructions and final tests remain unchanged.** Original
failures are retained; the repaired variant is not another generated task.

Sonnet 4.6 substitutes for Gemini 3.1 Pro. The original solver's provider adapter
requires bash tool calls and stops after empty failed responses. A successful
sampled trace supplies the derived replay witness. See [patches](patches/README.md).

Estimated model usage: generation **$0.218139**, solver attempts **$1.890216**,
total **$2.108355**, excluding cloud. No training-quality or adversarial robustness
approval is claimed.

Commands: `run_smoke.sh`, `convert_compat.sh`, `probe_smoke.sh`,
`probe_extended.sh`, `recover_trace.sh`, `fix_verifier_dependency.sh`,
`run_solver_compat.sh`. Repairs retain this concrete task ID. Original,
interrupted and corrected evidence is in the [artifact snapshot](../ARTIFACTS.md).
