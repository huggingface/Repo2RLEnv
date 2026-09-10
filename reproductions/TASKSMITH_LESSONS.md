# Lessons for Tasksmith from the reproduction pilots

These are evidence-backed recommendations, not changes already made to Tasksmith.
The earlier `codex/dynamic-harbor-curation` branch already contains repair and
adversarial audit stages. Improve their diagnosis and acceptance decisions rather
than adding duplicate stages.

| Observation | Tasksmith improvement | Validation before adopting |
| --- | --- | --- |
| TMax solution reaches 50/51 tests but its image lacks pytest-timeout | Classify infrastructure/verifier dependencies separately from incorrect solutions; repair only the responsible layer | Unchanged instruction and tests, fresh native and Harbor success |
| SETA author says PASS, but whole-second timing produces a zero-duration reference result | Repeat reference execution for timing/state-sensitive checks; require measurable precision | Fresh repeated oracle runs and preserved failing original |
| Fake Python executable earns reward without installing requests | Audit learner-controlled tools and outputs used by the verifier | Counterfeit-tool candidate must fail while a real installation passes |
| Empty CSV and fabricated log counts earn reward | Verify semantic conservation and non-vacuous outputs, not just headers or markers | Empty/truncated/fabricated outputs fail; legitimate alternatives pass |
| Empty encrypted archive earns reward | Check required behavior, including decryptability/content equality when encryption is requested | Touch-only candidate fails; real round-trip succeeds |
| Exact hidden diff/log formatting rejects reasonable solutions | Map each assertion to an explicit instruction requirement or invariant; test equivalent valid representations | Multiple legitimate implementations pass without access to hidden formatting |
| 16 exports, 12 execution successes with repairs, but incomplete quality approval | Keep export, reproducibility, solvability, discrimination and quality statuses separate | Reports and selection code cannot promote execution success to training approval |
| CLI-Gym awards success when its malformed runner yields an empty parsed test set | Require nonempty expected test identities and successful collection before grading task behavior | Empty output, missing tests and runner errors must fail explicitly |
| DataArc's static checks accept a reference that does not export the function its tests import | Execute the exact reference against the exact packaged verifier before solver rollouts | Reference must pass its API contract and all required assertions |
| SWE-Next emits dataset rows even when neither revision produces positive test contrast | Gate environment export on recorded execution evidence rather than row existence or process exit code | Reject empty F2P/P2P metadata and collection failures |
| TerminalWorld drops a command field between stages and cleanup relocates a Docker COPY asset | Validate stage payloads and final build context after each transformation | Required fields and assets survive serialization and packaging; rebuild the exported artifact |
| Small pre-filter caps miss all eligible changes in Pyramid | Bound expensive execution after native eligibility filtering; record counts before and after each filter | A known eligible change remains reachable under the pilot selection policy |
| TerminalWorld reaches reference/no-op success before exhausting its turn budget | Resume from typed intermediate receipts with a bounded repair budget | Preserve successful trials and complete missing checks without regenerating the task |
| harden-v0's first attacker solves the task legitimately while a known shortcut still exists | Include fixed regression attacks alongside generative red teaming | Previously demonstrated shortcuts must fail before robustness approval |
| SEC-bench image builds but lacks SecVerifier's agent runtime executable | Check builder-agent runtime compatibility before any paid generation | A real controller startup smoke test succeeds using the final image |

Priority: classify failure evidence, apply a bounded repair to the affected layer,
then run concrete shortcut regressions and independent blind rollouts. A final
LLM review should interpret these receipts rather than substitute for execution.

The first pilot's cost is not an apples-to-apples comparison with the earlier
Tasksmith campaign: repositories, domains and sample sizes differ. Reusing known
healthy images and native commands is promising; quantify the benefit with a
controlled Tasksmith comparison before claiming a cost or yield improvement.

Evidence: per-pipeline `RESULTS.md`, the immutable [artifact receipt](artifacts.json),
and the original/corrected/adversarial traces described in [ARTIFACTS.md](ARTIFACTS.md).
