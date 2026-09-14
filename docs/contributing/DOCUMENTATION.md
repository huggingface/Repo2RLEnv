# Maintain the documentation

Write for someone using or extending Repo2RLEnv. Describe the implemented behavior,
its limits and a runnable example. Put campaign diaries, provider receipts, budget
approvals and one-off input selections in ignored `workspace/` directories.

Each pipeline guide should explain its inputs, show one stage diagram, map model
calls to their inputs and outputs, and describe verification and bounded repair.
Link to canonical prompts, upstream credits, measured economics and the dataset.
Keep shared execution and quality contracts in their common guides and RFCs.

## Build and preview

```bash
python -m pip install -r requirements-docs.txt
mkdocs serve
mkdocs build --strict
```

No model keys, cloud accounts, private campaign directories or installed research
repositories are needed. The build reads owned source files as text; it does not
execute task code.

The [MkDocs pre-build hook](https://www.mkdocs.org/dev-guide/plugins/#on_pre_build)
generates `docs/pipelines/prompts/` from canonical templates and request-assembly
code. That directory and `site/` are ignored. Edit the canonical prompt or the
generator, then rebuild; do not commit copies of generated pages. The detailed
reference remains available in the built site, while each guide summarizes the
effective model request and links to the source for GitHub readers.

## Update measured results

Review the evidence and update `docs/data/pipelines.json`, then run:

```bash
python docs/_tools/generate_metrics.py
python docs/_tools/generate_metrics.py --check
```

Keep only sanitized aggregate measurements and public dataset revisions in this
file. Count retries under their candidate identity, separate retained tasks, and
keep generation, quality evaluation and solver outcomes distinct. Costs must
name their sample and include failures; unavailable compute or yield is not zero.
Keep raw evidence locally or with an appropriate dataset/release artifact.

Historical native-pipeline records live in the same file under `native_history`.
Preserve their evidence scope: a cached inventory, a generation-time verification
stamp and a Harbor oracle gate are different observations. Do not carry a gate
from an older cohort onto a newer dataset. The old synthesis counters are
run-cumulative; sum one final counter per run, not every exported task's counter.
Update [native results](../pipelines/native_results.md) when changing those
measurements, and retain source hashes or pinned public manifests.

## Review changes

Check the stage diagram against the implementation, validate example configs and
build the site from a clean checkout. CI checks the generated prompt references,
metrics tables, links and site build. Inspect changed diagrams in a browser.
Preserve upstream notices and source pins when condensing a guide or RFC.
