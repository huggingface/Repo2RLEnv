# First five upstream reproductions

Implementation branch: `codex/upstream-reproductions`. Authorized September 10, 2026:
**a new $500 reproduction budget**, separate from the older Tasksmith campaigns.
OpenAI and Anthropic keys may be used from `.env`; Modal and Daytona provide remote
execution. Never build images or run generated tasks on the local machine.

## Deliverable and sequence

Get these released implementations working, in order, with executable Harbor exports:

1. SETA Seed2Synth: published seed → original Claude author → native Harbor task.
2. SETA Evol: existing task → original evolution strategy → native Harbor child.
3. Endless Terminals: original generator → Apptainer bundle → upstream Harbor converter.
4. TMax: original skill/fixture sampler → Apptainer bundle → upstream Harbor converter.
5. SWE-smith: supported healthy repository → original mutation and validation → issue
   description → minimal Harbor adapter.

First establish a working smoke run for each, then repeat on five inputs before scaling.
The eventual target remains 100 tasks per generator; small runs do not satisfy that target.
Do not spend the whole budget scaling the first experiment before establishing the other
four. Retain every failure and move past an external blocker only with evidence recorded.

## Stage contracts

| Stage | Implementation goal | Evidence required to advance |
| --- | --- | --- |
| Freeze | Pinned upstream source and ordered native input subset | Commit, license, input IDs/hashes, native settings, deviations |
| Bootstrap | Remote Linux worker with upstream dependencies | Docker smoke; Apptainer smoke before Endless/TMax; dependency versions; resource ID and teardown |
| Generate | Run the original entry point, prompts, agents and acceptance logic | Exact command, logs, actual output files, native verdict; failures retained |
| Export | Preserve task semantics in Harbor | Harbor schema parses, assets complete, converter identity, source/export hashes |
| Validate | Run the exported task in fresh containers | No-op and reference/witness outcomes, test execution evidence; infrastructure errors never count as passes |
| Compare | Native and exported verifiers agree on equivalent states | Test identities/results and reward comparison; missing upstream oracle explicitly recorded |
| Inspect | Assess usability independently from reproduction | Instruction/test review and a small blind solver sample; leakage and shortcuts recorded |
| Repeat | Establish repeatability and measured cost | Separate attempted/emitted/accepted/exported/parity/audit counts; forecast before scaling |

## Requirements and initial verified state

- Local controller: existing Python virtual environment, Modal 1.5.5, Daytona 0.198.0,
  Harbor 0.20.0, Hugging Face Hub 1.13.0. Local tools coordinate and inspect files only.
- Credentials present: OpenAI, Anthropic, Daytona, HF token; Modal configuration exists.
  Presence is not proof of provider authentication; remote bootstrap tests establish that.
- Remote worker: CPU Linux VM, Docker daemon and Compose, git, Python, isolated upstream
  virtual environments; Apptainer added and tested for experiments 3–4.
- Model fidelity: SETA Seed2Synth names `claude-opus-4-6`; Evol names
  `claude-sonnet-4-6`. Preserve both. Record any
  API model substitution required by other projects; this is workflow reproduction,
  not a claim to match their published model configuration or training result.
- Both prior reports are copied byte-for-byte into [reports](reports/README.md).

## Implementation boundaries

Shared code only launches remote workers, limits resources, preserves receipts and maps
artifacts to Harbor. It does not author tasks or replace upstream agents with Tasksmith,
Pi, OpenCode or LangGraph. Keep upstream code in ignored `upstream/` checkouts and store
minimal compatibility/resource patches in each experiment's `patches/` directory.
Disable uploads into upstream-owned Hugging Face repositories.

Native, exported and audit artifacts stay separate. For TMax, preserve its absence of
a generated oracle; an original successful sampled trajectory can be a separately
identified replay witness. For SETA, an author-written PASS report is native acceptance,
not an independent execution result. No new verifier or acceptance method is introduced
to conceal an upstream weakness.

## Spending and recovery

Use a separate ledger under ignored `reproductions/runs/`, initialized at $500. Reserve
cloud and inference allowance before paid execution. Keep unresolved reservations until
usage is reconciled; label cloud estimates and model-reported costs separately. Set
remote wall-clock limits and per-call agent limits. Save sandbox IDs immediately so a
controller interruption does not strand an untracked worker. Collect artifacts and
terminate idle workers. Repeated failures require a diagnosis before retrying.

Initial planning envelopes: $35 shared bootstrap, $70 each for SETA synthesis/evolution,
$90 each for Endless/TMax, $85 SWE-smith, $60 common validation/contingency. These are
planning allocations, not charges or rigid per-project entitlements. Scale only from
observed cost and remaining allowance.

Update each experiment's `RESULTS.md` and `experiment.json` with measured outcomes.
Never count downloaded fixtures, duplicate exports or scaffold files as newly generated
environments. Commit the runnable recipes, patches, summaries and artifact references;
keep secrets, full upstream checkouts and bulk run outputs untracked.
