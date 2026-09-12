# RFC 0028: Tasksmith PR pilot

Status: implemented; live pilot in progress. This follows the owned recipe implementations and RFC 0027.

## Goal

Turn each supplied public Python PR into a faithful, standalone Harbor task. Start with five small CPU PRs, including huggingface_hub and smolagents. Freeze the input denominator before generation. Keep every failed attempt and do not replace a difficult PR with an unrelated task.

## Implementation milestones

1. **Intake:** pin repository, head, base and full diff; reject unsupported changes explicitly. Validation: all executable source edits are represented in the task lineage.
2. **Investigation:** Pi or OpenCode inspects the pinned repository through remote shell tools and submits a typed dependency/test profile. Validation: bounded tool calls, no controller filesystem tools or provider credentials in the agent process.
3. **Bootstrap:** use the existing remote Docker bootstrap on Modal or Daytona. Reuse dependency layers on a shared worker. Validation: the selected merged-head tests pass offline, with receipt and logs. Repairs to instructions/tests must not repeat bootstrap. Cache lifetime and actual hits are reported.
4. **Design:** submit a human-facing request, requirement/test mapping, and any necessary behavioral tests. Prefer existing PR tests; add tests for weak coverage. Validation: observable requirements, no algorithm/patch disclosure, preserved source behavior.
5. **Construction:** reverse only the PR source diff, keeping head tests private, and emit using the owned repository Harbor exporter. Validation: an actual fail-to-pass contrast plus pass-to-pass coverage; no git history, private tests or changelog answers in the learner image. This first profile supports changes to existing Python files.
6. **Quality:** reuse the bounded component review/repair loop and Sonnet rollout. Validation: baseline fails, oracle passes, wrong solution fails, valid alternative passes, and the reviewer finds task/verifier sound with a legitimate rollout. This is `practical-generation-v1`; legacy strict acceptance is unchanged.
7. **Pilot:** complete one PR end to end, then the other four with the same pipeline. Report generated and usable separately, repairs, dependency reuse and stage costs. No manual task edits.

## Execution and durability

LangGraph owns stage routing and SQLite checkpoints. Owned Pi/OpenCode adapters own coding-agent sessions. Typed stage artifacts are committed before graph advancement; completed artifacts are reusable only with matching inputs. An interrupted model effect is retained for reconciliation, never silently redispatched. Remote jobs use the existing detached job supervisor. All reservations use the existing campaign SQLite budget ledger, with a bounded pilot allowance. Cleanup retains uncertain costs until reconciled from evidence.

No target builds, imports, tests or Docker commands run on the controller. Only metadata lookup, artifact serialization, orchestration and tests of our own code run there. Both providers use the same `RemoteWorker` contract. Learner and verifier have no network and are separate containers.

## Scope and attribution

Reuse our own adapters from the previous Tasksmith branch, existing `bootstrap`, `execution`, repository exporter and `quality.loop`. LangGraph, Pi and OpenCode remain ordinary pinned libraries; no research repository is imported or installed. PR regression contrast is inspired by SWE-bench/SWE-gen; source reconstruction by R2E; focused probes and iterative construction incorporate the reproduction findings documented in `tasksmith_pilot_learnings.md`.

GPU, added/deleted source files, non-Python changes and multi-service tasks remain explicit unsupported profiles, not silently simplified tasks. This pilot measures five concrete inputs; it cannot establish universal PR conversion yield.

## Pilot-driven corrections

The first live attempt exposed a missing README in the filtered learner image. Bootstrap now builds that public workspace before design/review. Initial reviews also missed unittest methods when a multiword literal search returned no matches; selected test identities and actual build-error messages now enter the initial evidence pack. Tasksmith locks the PR oracle and learner source against quality repairs. Supplemental test evidence is extracted from the added private source text, and a generation-reuse mode supports current review policies without repeating successful generation. Instruction review explicitly checks for implementation hints, even when filesystem isolation passes.

The independent review found two further orchestration defects: demanding semantic probes before serving requested code reads, and exhausting probe slots without a valid-alternative control. Reads now precede probe validation, retained controls are visible to the reviewer, and Tasksmith allows four probes with required coverage of both kinds. Context selection prioritizes the selected test class and exact API symbols, avoiding unrelated `test_empty` methods and broad substring matches. Generation reuse checks both bundle integrity and PR provenance and carries forward prior counterexamples.

HF repository evidence exposed duplicate inventory overhead; hashes now stay in local evidence records and the model receives compact inventories. Smolagents exhausted a retry rediscovering its profile, so retries now receive their previous artifact and reserve the final two model calls for submission. Click exposed an invented instruction constraint that conflicted with the PR implementation. Quality review now receives private original PR intent and diff, and generation reuse rebuilds reference-conflicted drafts instead of carrying their invalid framing into a new review.
