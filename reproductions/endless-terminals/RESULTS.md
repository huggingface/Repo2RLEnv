# Endless Terminals reproduction

On September 10, 2026 the original generator attempted **11 candidates**, emitted
**5 native bundles**, and the original Docker converter converted all five.
One genuine upstream solver replay passes native and Harbor tests. **None of
these five is approved for training:** inspection found concrete quality failures.

| Task suffix | Execution result | Quality finding |
| --- | --- | --- |
| `48ab827c` | Original GPT-4o success; Harbor no-op 0 / replay 1 | A fake Python executable also earns reward 1 without installing requests or creating a virtual environment |
| `d0040459` | Native and Harbor no-op both pass | Starting image already contains the exact expected final diagnostics |
| `01646bc7` | Native solver 4/6 tests; no valid witness | Instruction asks for at least three context lines; verifier requires an exact one-context-line diff and undisclosed header format |
| `bf61fda1` | Native solver 4/5 tests; no valid witness | Hidden timestamp formatting requirement; a zero-byte encrypted file also earns reward 1 |
| `eb94a8e1` | Native solver fails; no valid witness | Header-only CSV and fabricated row counts earn reward 1; undisclosed exact log confirmation |

Adversarial candidates are **invalid solutions**, stored outside normal task
exports. Reward 1 counts as a verifier failure, never a successful reference.
The first encryption diagnostic had a quoting error; `adversarial-02` corrects
our script and demonstrates the shortcut against unchanged instructions and tests.

The first smoke failed its container build (`chown user:user` without creating
that user). Subsequent batches emitted 2/5 and 3/5. All failures remain recorded.

Estimated model usage: generation **$0.285095**, conversion **$0.026255**, native
solver samples **$0.1899025**; total **$0.5012525**, excluding shared cloud compute.
These are usage estimates, not invoices.

The original CLI's GPT-4o default replaces the README's Qwen3-32B/vLLM deployment.
Original prompts, acceptance rules and tests remain intact. Compatibility covers
the API endpoint, build evidence, missing persistent SIFs and Apptainer user
namespaces. See [patches](patches/README.md).

Run the smoke, batch, conversion and probe recipes, followed by the corresponding
`*_batch_02.sh` recipes. `audit_shortcuts.sh` preserves the concrete counterexamples.
Original tasks, failures and traces are in the [artifact snapshot](../ARTIFACTS.md).
