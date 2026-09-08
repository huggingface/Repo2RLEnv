# Curation regression corpus

`tests/test_curation_regressions.py` preserves six verifier failures as small,
executable behavioral examples. The fixtures retain the PR, exact task digests,
source-file hashes, and selected protected assertions or observer functions.
They do not depend on the ignored `workspace/` directory at test time.

Run the complete local corpus with:

```bash
uv run --no-sync --with numpy==2.2.6 pytest -q tests/test_curation_regressions.py
```

The artifact-finalization case also needs the project's `curation` extra
(`harbor` supplies the task-config schema). The test reports a skip when that
extra is absent. Without NumPy, the numeric/orientation cases report skips;
those skips are **not** a complete corpus pass. A complete run currently has
15 passing tests, including six fixture-integrity checks.

The suite executes only protected NumPy/stdlib definitions, independent response
simulators, and the existing artifact finalizer on temporary files. It never
imports a target repository or Torch, executes a worker program, builds an image,
calls a model, or runs cloud validation. The finalizer uses a previously pinned
Docker recipe, and its test blocks network access.

## Behavioral witnesses

| Case | Historical failure and local distinction | Limit |
| --- | --- | --- |
| `wrong_dora_norm` — [PEFT #2661](https://github.com/huggingface/peft/pull/2661) | A recorded remote Gold-derived mutant returning unit DoRA norms earned reward **1**. The retained cache assertions accept both correct and wrong outputs when cached and uncached results agree. A fixed-magnitude matrix example produces different independent normalized outputs and rejects the same wrong calculation. | The fixed-magnitude assertion is a new corpus witness, not an admitted corrected task. Local execution does not rerun the recorded remote trial. |
| `projection_orientation` — [PEFT #2575](https://github.com/huggingface/peft/pull/2575) | Exact v4/v5 protected tests exercise four original rotation modes. The old assertion rejects `(W @ rotated_rows.T).T`; the corrected assertion accepts it alongside ordinary and split/reordered projections. Both still reject a changed projection weight even when the reported final output is correct. | This executes protected assertions over algebraically consistent observations, not the Torch equivalent control. |
| `package_artifacts` — [PEFT #2575](https://github.com/huggingface/peft/pull/2575) | A real v5 Contract lists only three files despite permitting sibling helpers. Finalizing it omits `rotation.py` and `__init__.py`. The v6 package-level scope includes both and deletes the baseline package in the grading image. An unrelated package remains excluded. | The omission was identified statically; there is no claimed paid helper-trial failure. Temporary tests and solution files are inert finalizer scaffolding. |
| `labels_shape_reference` — [TRL #5407](https://github.com/huggingface/trl/pull/5407) | Old error fixtures accept an implementation that skips labels-shape validation when prompt metadata is present. The retained corrected fixture distinguishes that branch. An independent list-based implementation with the unconditional guard passes both versions and the valid prompt case. The old/new Gold hashes record the actual guard addition. | No target function is executed; the response simulator isolates this branch and does not validate the rest of the distillation API. |
| `optional_required_support` — [PEFT #2939](https://github.com/huggingface/peft/pull/2939) | A completed score-3 review with no blockers put a missing required support check in `repairs`. Moving `supports_lora_conversion=True` onto the common LoRA parent preserves the retained old synthetic-hook checks but fails the corrected Embedding/Conv2d checks. Correct inheritance passes both. | **Review replay required.** The stdlib inheritance witness demonstrates the missing behavior; it cannot prove a future reviewer classifies it correctly. No test passes merely because a feedback label matches. |
| `backward_context_storage` — [TRL #5575](https://github.com/huggingface/trl/pull/5575) | The exact old hook observer misses a vocabulary allocation retained on a context attribute. The corrected live-storage census observes it above the original budget, counts aliases once, excludes fixture storage, and does not keep released tensor doubles alive. A bounded chunk allocation remains below budget. | **Remote replay required.** Storage-protocol doubles validate accounting only, not Torch interception, autograd lifetime, native allocations, or a universal backward peak. The triggering review was incomplete and is not represented as a completed verdict. |

## Provenance and interpretation

Each JSON fixture under `tests/fixtures/curation_regressions/` records:

- The source PR and immutable base/head revisions.
- Full historical task digests, checked against the retained directories when
  this corpus was assembled.
- Original project-relative evidence paths and SHA-256 hashes of complete files.
- Selected definitions with separate text hashes. Definitions are copied intact
  apart from dedenting; imports and whole worker modules are never evaluated.
- Relevant JSON projections, the local witness scope, and explicit replay limits.

The fixture-integrity checks detect accidental excerpt changes; a stored hash
alone does not reconstruct or authenticate every historical task file. Original
paths are provenance pointers, not runtime dependencies. Task acceptance, solver
success, and review quality must not be inferred from these local tests.

For the reset pilot, run this corpus before spending on authoring or validation.
Treat failures as harness/witness regressions to investigate. Passing it establishes
only these bounded distinctions; it is not an admission shortcut or permission
to continue repairing an old candidate. Review and remote cases need separately
budgeted replay within the pilot's authorized limits.
