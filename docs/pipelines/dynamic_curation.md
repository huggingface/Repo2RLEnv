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

## Admission

A score cannot override failed mandatory gates:

1. Validate Harbor configuration, submission paths and requirement-to-test mapping.
2. Baseline earns zero; oracle earns one on at least three fresh trials by default.
3. Tampering and at least two author-supplied behavioral mutations earn zero.
4. Sonnet and Opus complete real Harbor trials; infrastructure errors reject the run.
5. A dedicated adversarial solver attempts to earn reward without implementing the task.
6. An independent reviewer reads task files and trajectories and returns eight
   scored criteria with evidence, blockers, failure attribution and repair suggestions.
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
filesystem. Pytest executes as the unprivileged agent user with external plugins
and repository configuration disabled. Missing reports, insufficient collected
tests, skips, errors and timeouts yield zero. The default reward is binary and
deterministic; curation scores never become solver rewards.

This is a defense-in-depth design, not a proof against all malicious Python code.
In-process test runners can still be attacked by submitted code. Adversarial
rollouts and human inspection remain important. Judge-based tasks are deferred from
the default deterministic campaign. An explicit opt-in Harbor verifier is available
as `repo2rlenv.curation.judge_reward:JudgeRewardVerifier`: configure
`verifier.import_path` in the Harbor job and pass `budget_path`, `budget_limit`, and
`model` in `verifier.kwargs`. The task must use a separate verifier and supply
`tests/judge_reward.json` with `justification`, named `criteria`, relative text
`artifacts`, and `threshold`. Deterministic prechecks run first; the host-side judge
then grades only the declared artifacts and records its model, cost and rationale.
These rewards are explicitly nondeterministic and never silently enabled.

## Evidence and costs

`manifest.json` lists admissions and rejections; `tasks/` contains admitted Harbor
tasks. Each candidate has source provenance, author trace, exact drafts, Harbor
trial output, independent review and a verdict. Full solver traces are recorded
in each trial's `agent/trace.jsonl`. `budget.json` is a process-safe write-ahead
ledger of API reservations, metered model cost and conservative cloud allowances.
Unknown model pricing fails closed. Provider invoices remain the billing authority.

`publish_evidence()` can archive a campaign to a private Hugging Face bucket under
a content-addressed prefix, with SHA-256 checksums and no solver filesystem exports.
It rejects admitted tasks changed since their review. No secret is passed into an
author or solver sandbox. Publishing public benchmark tasks should include the
review evidence and accurately report the number admitted, model versions,
attempt counts, failures and unresolved limitations.
