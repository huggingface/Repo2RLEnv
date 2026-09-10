# SETA Seed2Synth reproduction

The original Opus 4.6 author produced **three native PASS tasks from three frozen
Unix.SE seeds** on September 10, 2026. All three export as byte-preserving Harbor
tasks. Two independently pass unchanged; one has a flaky reference.

| Task | SDK-reported generation cost | Fresh Harbor no-op / reference |
| --- | --- | --- |
| `unix-se-26047`: shell PATH setup | $3.14944225 | 0 / 1 |
| `unix-se-73498`: shell history | $2.11776475 | 0 / 1; nine tests |
| `unix-se-52313`: pipeline timing | $2.020925 | 0 / 0; reference reports zero elapsed time |

Total generation cost reported by the SDK: **$7.288132**, not a provider invoice.
The timing reference subtracts whole-second timestamps for a subsecond pipeline.
A separate reference-only variant retains nanosecond precision and passes **three
fresh no-op/reference runs**. Instruction, image and verifier are byte-identical.
The original failure is preserved; the variant is not an extra generated task.
See `fix_reference_precision.sh`.

The PATH task also passes an offline image variant, then a **blind Sonnet
Terminus 2 rollout**: reward 1, six episodes, $0.0711717 estimated. The trace fixes
shell startup files and scripts and checks real behavior without reading hidden
tests or the reference. Several tests depend on output markers, so this establishes
usability rather than reward-hack resistance or training-quality approval.

The published seed dataset returned 403 with and without the available HF token.
We froze an independent Unix.SE Q&A sample through the original source adapter,
retaining attribution and content licenses. This does **not** reproduce the gated
published split. See [deviations](patches/README.md).

Offline validation required a narrow Harbor sidecar compatibility patch for the
Modal VM's missing nft_fib_inet. The task image also pre-caches exact verifier
dependencies, puts installed uv on PATH and sets UV_OFFLINE. Instructions and
Python tests remain unchanged; failed startup/dependency probes remain recorded.

Commands: `run_smoke.sh`, `validate_smoke.sh`, `run_offline_v2.sh`,
`run_repeat.sh`, `fix_reference_precision.sh`. Native traces, baseline and repaired
tasks, independent receipts and the blind trajectory are in the
[artifact snapshot](../ARTIFACTS.md).
