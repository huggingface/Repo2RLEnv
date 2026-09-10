# SWE-smith reproduction

The pinned procedural generator ran on its supported
`mewwts__addict.75284f95` profile on September 10, 2026. It emitted **23 mutations**.
The first sampled **10 patches all passed native validation** with at least one
FAIL_TO_PASS test and no timeouts. The original issue writer generated five
nonempty descriptions; all five were exported to Harbor.

All five exports pass independent fresh-container **no-op 0 / reference 1**
checks. References reverse the original mutation. The adapter restores pristine
tests and grades the exact native FAIL_TO_PASS/PASS_TO_PASS cases, using the
profile's own test command. Images use the observed immutable repository digest.

A blind Sonnet Terminus 2 audit also solved one offline task: **reward 1**, four
episodes, no exception. The audit image adds tmux only. Issue generation cost was
**$0.018772 estimated**; the blind audit was **$0.0412026 estimated**. Cloud costs
are shared and separate. These are API usage estimates, not provider invoices.

The GitHub-publishing gather step is replaced with local projection of native
validation reports. This avoids publishing upstream branches and handles the
pinned gather/validator orientation mismatch. Mutation generation, native
validation and issue prompts are unchanged. GPT-5-mini uses the direct OpenAI
endpoint rather than Portkey. See [deviations](patches/README.md).

This is one supported repository and the procedural mutation path. It does not
establish arbitrary repository support, LLM mutation reproduction or 100 tasks.

The inspected blind trace repairs `Dict.update` and exercises actual behavior;
it does not read hidden tests or the solution. The issue gives a strong localization
hint. This panel establishes workflow correctness, not benchmark difficulty or
adversarial robustness.

Commands: `run_native.sh`, `run_issues.sh`, `validate_harbor.sh`,
`run_blind_audit.sh`. Native reports, all five Harbor task folders, independent
receipts and the blind trajectory are in the [artifact snapshot](../ARTIFACTS.md).
Third-party notices:
[SWE-smith](UPSTREAM_LICENSE), [addict](ADDICT_LICENSE).
