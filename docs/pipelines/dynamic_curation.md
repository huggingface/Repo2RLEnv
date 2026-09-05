# Dynamic PR curation (experimental)

`repo2rlenv curate` gives a LangGraph author a remote shell, repository evidence and
a Harbor validation tool. It explores, writes a task, runs it, and repairs it from
execution feedback. A separate reviewer reads the specification and solver traces.
See [RFC 0011](../rfcs/0011-dynamic-curation.md) for the design and research basis.

## Run

```bash
uv sync --extra curation --group dev
modal token set  # only if Modal is not already configured
# Set ANTHROPIC_API_KEY; gh auth login supplies GitHub access.

uv run --extra curation repo2rlenv curate \
  --pr https://github.com/huggingface/accelerate/pull/3969 \
  --target 1 --budget-usd 30 --out workspace/pilot

uv run --extra curation repo2rlenv curate \
  --seeds seeds.md --target 30 --budget-usd 450 --out workspace/campaign

# Inspect the plan without model calls or sandbox creation:
uv run --extra curation repo2rlenv curate --seeds seeds.md --plan
```

`--repo owner/name` discovers recent merged PRs. `--config config.json` accepts
the fields in `CampaignConfig`: author/judge/solver models, attempts, time limits,
oracle repetitions, revisions, quality threshold and spending limits. The default
solver models are Sonnet 4.6 and Opus 4.6, with one attempt each; increase
`solver_attempts` for meaningful success-rate estimates. One attempt is diagnostic,
not a precise difficulty estimate.

Re-run the same command to skip completed candidates. `--retry-rejected` explicitly
retries rejections while retaining prior evidence and costs. Configuration changes
require a separate campaign directory. An interrupted candidate restarts in a fresh
sandbox; previous reservations remain charged until reconciled. The CLI exits 2
when it ends short of the requested target, including budget exhaustion.

## Compare authoring runtimes

Set `author_runtime` to `langgraph`, `pi`, or `opencode` in a campaign config.
Pi and OpenCode require Node >=22.19 and the pinned packages next to their adapters:

```bash
npm ci --prefix src/repo2rlenv/curation/runtimes
uv run --extra curation repo2rlenv curate --compare-runtimes \
  --pr https://github.com/huggingface/accelerate/pull/3969 \
  --pr https://github.com/huggingface/peft/pull/2661 \
  --pr https://github.com/huggingface/trl/pull/6066 \
  --config configs/curation/runtime-comparison.json \
  --out workspace/runtime-comparison
```

All three authors run concurrently for each PR, using a shared $90 ceiling and
$10 per cell in this example. Only the authoring runtime varies; the source,
instructions, remote tools, model, token/turn limits, solver panel and judge are
fixed. Runtime labels are omitted from the judge's evidence paths. Reports retain
unscored and failed candidates alongside admissions; missing scores never become
zero scores. `comparison.json` holds the machine-readable results and
`comparison.md` the summary. Re-running the identical command skips completed cells
and preserves interrupted attempts and their spending.
Use `--retry-rejected` to retry comparison cells with execution or infrastructure
failures after a repair; earlier
outcomes remain in attempt history and the same per-cell budget still applies.
Resume records harness changes and preserves resolved source revisions. Accepted
cells are checked against their task digest and admission protocol before reuse.

This is a comparison of controlled adapters, not each product's unrestricted
defaults: local tools, resource discovery, automatic retries, compaction and
auxiliary agents are disabled. Sessions and native events remain available.
Pi and OpenCode receive only an ephemeral loopback token; the Python bridge holds
the real provider key, meters inference and forwards the allowed cloud tools.
The Python controller, verification protocol and solver implementation stay common.
Three PRs provide diagnostic evidence, not a statistically established winner.

Supported Sonnet/Opus models use explicit adaptive thinking at medium effort and
a 16,000-token response ceiling across all three adapters. Thinking tokens share
that ceiling with visible responses. Empty or truncated responses are execution
failures, never completed zero-reward rollouts. Model settings are recorded in
traces and bound to solver/adversary evidence; changing the policy reruns those
trials while retaining compatible deterministic checks.

The first supported external-provider protocol is Anthropic. Other providers
remain supported by the LangGraph adapter through LiteLLM; additional external
protocols require explicit adapters and matching budget accounting.

## Admission

A score cannot override failed mandatory gates:

1. Validate Harbor configuration, submission paths and requirement-to-test mapping.
2. Baseline earns zero; oracle earns one on at least three fresh trials by default.
3. Tampering and at least two author-supplied behavioral mutations earn zero.
   At least one meaningful alternative valid implementation earns one. This
   positive control tests whether the verifier accepts solutions beyond the PR.
4. Sonnet and Opus complete real Harbor trials; infrastructure errors reject the run.
5. A dedicated adversarial solver attempts to earn reward without implementing the task.
6. An independent reviewer reads task files and trajectories and returns eight
   scored criteria with evidence, blockers, failure attribution and repair suggestions.
   The evidence includes oracle patches and changed solver/adversary submissions
   compared with baseline exports. Omitted or oversized evidence is recorded explicitly.
7. Admit only with every criterion passed, no unresolved failures/hacks, score at
   least 85/100, and evidence bound to the exact task digest.

The full custom rubric is in `src/repo2rlenv/curation/rubric.toml`; it can also be
passed to `harbor check TASK -e modal -r RUBRIC`. Harbor 0.20 runs that static check
as a Harbor job. Its reward confirms a valid review was produced; read the actual
criterion results to determine quality. The curation reviewer additionally sees
rollouts and does not mistake check-job reward for task acceptance.

All admitted tasks are marked `human_review: pending`. This reflects the paper's
warning that automated review misses errors; it is not a claim of human certification.

## Isolation and reward

The initial profile targets CPU-verifiable Python repository behavior. Native or
GPU-only tasks should be deferred unless the author can demonstrate an honest
supported contract. It does not replace numerical GPU evidence with mock assertions.

Model calls happen in the controller. Provider-enforced network blocking covers
the entire solver and verifier lifetimes. Dependencies and assets are fetched
while building the image; no broad Hugging Face domain allowlist is used. Solver
images remove Git history, have no oracle/tests/API keys, and run the agent as a
non-root user. Runtime probes check these properties, including IP-level egress.

Harbor copies only the declared source paths into a fresh grading image. The test
runner, dependency environment and reward writer are independent of the solver's
filesystem. Protected pytest runs in its own virtual environment containing only
pytest, numpy and standard Python. It never imports the editable repository.
Tests use `from probe import run_probe` to call submitted code in a separate
unprivileged process and receive bounded JSON observations; assertions and expected
results stay in the protected process. Workers cannot read `/tests`, modify that
virtual environment, or write the reward. Repository pytest plugins and configuration
are disabled. Missing reports, insufficient collected
tests, skips, errors and timeouts yield zero. The default reward is binary and
deterministic; curation scores never become solver rewards.

This boundary closes direct pytest monkeypatching and report forgery from submitted
Python. It does not prove that a task's behavioral coverage excludes every shortcut;
adversarial rollouts and human inspection remain important. Judge-based tasks are deferred from
the default deterministic campaign. An explicit opt-in Harbor verifier is available
as `repo2rlenv.curation.judge_reward:JudgeRewardVerifier`: configure
`verifier.import_path` in the Harbor job and pass `budget_path`, `budget_limit`, and
`model` in `verifier.kwargs`. The task must use a separate verifier and supply
`tests/judge_reward.json` with `justification`, named `criteria`, relative text
`artifacts`, and `threshold`. Deterministic prechecks run first; the host-side judge
then grades only the declared artifacts and records its model, cost and rationale.
These rewards are explicitly nondeterministic and never silently enabled.

The JSON interface deliberately limits this first profile. Tests requiring arbitrary
Python object exchange need an explicit serializable observation contract or should
be deferred. Sending a worker a script containing assertions is rejected: untrusted
code could neutralize those assertions. Batch related inputs to avoid paying Python
and torch import overhead for every individual example.

## Evidence and costs

`manifest.json` lists admissions and rejections; `tasks/` contains admitted Harbor
tasks. Each candidate has source provenance, author trace, exact drafts, Harbor
trial output, independent review and a verdict. Full solver traces are recorded
in each trial's `agent/trace.jsonl`. `budget.json` is a process-safe write-ahead
ledger of API reservations, metered model cost and conservative cloud allowances.
Unknown model pricing fails closed. Provider invoices remain the billing authority.
Interrupted validation can resume from an unchanged finalized task, reusing
successful checks and rerunning missing or invalid trials. A pending review can
resume without repeating authoring or cloud validation; a valid quality rejection
requires task repair. Structured-review formatting gets at most one budgeted
finalization call that preserves the evidence and findings already collected.

`publish_evidence()` can archive a campaign or runtime comparison to a private
Hugging Face bucket under a content-addressed prefix, with SHA-256 checksums.
It excludes raw solver exports and runtime credentials/caches, while preserving
native transcripts and the bounded changed text inspected by the reviewer in
`review-submissions.json`.
It freezes files before hashing and uploading, refuses an active campaign, and
rejects admitted tasks changed since their review. No secret is passed into an
author or solver sandbox. Publishing public benchmark tasks should include the
review evidence and accurately report the number admitted, model versions,
attempt counts, failures and unresolved limitations.
