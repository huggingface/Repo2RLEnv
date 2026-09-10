# Task artifacts and evidence

Generated task files and full traces live in the owner's private Hugging Face
artifact dataset. The repository keeps recipes, source pins, compatibility
patches and measured summaries. This follows the project's artifact convention
in `CLAUDE.md`; no generated datasets or container images are committed to Git.

Owner access: [Hugging Face artifact dataset](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-upstream-reproductions-20260910).
Other contributors can reproduce the recipes without access to these private
traces. Public task distribution has not been performed in this pilot.

`artifacts.json` records the immutable dataset revision, archive path and SHA-256.
Authenticate with an HF token that can read the dataset, either through the usual
HF login or `HF_TOKEN` in the environment/project `.env`, then run:

```bash
.venv/bin/python reproductions/runtime/fetch_artifacts.py \
  reproductions/artifacts.json reproductions/runs/reproduction-artifacts
```

The download contains definitions and logs only. Build images and run Harbor
tasks on the remote worker described in [RUNBOOK.md](RUNBOOK.md).

The expanded reproduction batch is stored separately under `phase02/`, preserving
the original batch and its immutable receipt. It contains 13 task definitions
representing 12 additional environments (two CLI-Gym instructions share one
environment), plus component outputs and failures. Nine additional environments
pass execution contrast; all remain `training_approved: false`.

```bash
.venv/bin/python reproductions/runtime/fetch_artifacts.py \
  reproductions/artifacts-phase02.json reproductions/runs/expanded-artifacts
```

The 3,980-file snapshot preserves historical recipe uploads alongside native and
exported outputs. TerminalWorld source recordings and Docker/SIF images are
excluded. Expanded task manifests, stage journals, settlements and teardown
receipts are adjacent to the archive under `phase02/` in the recorded revision.
One rejected CLI-Gym variant has no exact matching execution hash after Python
bytecode caches were excluded from the archive; that mismatch is retained in
the task index rather than silently normalized away.

The artifact repository separates:

- `tasks/`: original native Harbor exports and minimal packaging adapters,
  including failed or rejected tasks so the measured denominator is preserved.
- `variants/`: explicitly identified offline, reference-precision or dependency
  changes. These are not additional generated tasks.
- `task-index.json`: exact task hashes, matching execution receipts and quality
  findings. Every entry remains `training_approved: false` in this pilot.
- `evidence/`: the complete source/trace/receipt archive. Author traces, hidden
  tests, references and adversarial scripts are evaluation-side data; only the
  task's `environment/` directory belongs in the learner image.
- `notices/`: pinned upstream licenses and input provenance. The sources have
  different licenses; no blanket new license is asserted for the generated data.

The archive retains the observed baseline failures as well as corrected variants.
In particular, a reward-1 adversarial probe indicates a verifier weakness and must
never be counted as a successful solving trajectory. Re-run hashes and inspect
the accompanying quality status before selecting any task for training.
