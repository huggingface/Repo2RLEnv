# Curation pilot: author runtimes and verifier lessons

This is an engineering pilot, not a runtime leaderboard or a completed benchmark
release. The nine-cell comparison completed on September 5, 2026. None of its
tasks entered the final selection after the additional coverage audit.

## What was compared

LangGraph, Pi and OpenCode authored tasks for the same three public PRs:
[Accelerate 3969](https://github.com/huggingface/accelerate/pull/3969),
[PEFT 2661](https://github.com/huggingface/peft/pull/2661) and
[TRL 6066](https://github.com/huggingface/trl/pull/6066).
The comparison used the same model panel, source revisions, shell and validation
tools, prompts, limits and quality gates. Authoring and task execution ran on
Modal. Pi and OpenCode used their actual pinned packages through the metered
bridge, rather than Python simulations of those agents.

Interrupted attempts and protocol changes are retained in the evidence. Some
cells resumed from drafts after infrastructure fixes. This prevents interpreting
the results as a clean, single-version randomized experiment.

| Author | Completed cells | Automatic admissions | Quality rejections | Deferrals | Infrastructure failures | Budget stops | Final selected |
|---|---:|---:|---:|---:|---:|---:|---:|
| LangGraph | 3 | 1 | 0 | 0 | 1 | 1 | 0 |
| Pi | 3 | 0 | 1 | 0 | 0 | 2 | 0 |
| OpenCode | 3 | 0 | 0 | 1 | 0 | 2 | 0 |

Only two cells received final quality scores: 86.25 for the LangGraph admission
and 41.25 for the Pi rejection. Missing scores are not zero scores. A budget stop
is not evidence that a runtime could never produce a valid task. OpenCode's
deferral was a model decision; a separate protocol check did not establish a
tool-transport failure as its cause.

The automatically admitted task was held out after an independent audit found
missing numerical and API cases. A separately repaired version passed its
execution controls but failed the final quality gate. Earlier admissions under
superseded protocols also remain historical evidence, not selected tasks.

LangGraph was chosen provisionally for continued production because it completed
an admission path in this pilot. These observations do not establish statistical
superiority over Pi or OpenCode. A larger comparison needs more sources, stable
runtime versions, repeated authoring attempts and enough budget to reduce censored
results.

## Subsequent production selection

As of September 5, 2026, two distinct production tasks have been selected after
complete validation and additional agent review. Neither belongs to the matched
runtime pilot above. Human benchmark review remains pending.

| Source | Quality score | Deterministic trials | Sonnet / Opus reward | Adversarial attempt reward |
|---|---:|---:|---|---:|
| Diffusers 13226: confidence-aware loss | 90 | 13 | 1 / 1 | 0 |
| PEFT 3083: transposed expert LoRA parameters | 92.5 | 12 | 1 / 1 | 0 |

Both released directories match the exact digest used for their trials and final
review. These are moderate numerical implementation tasks, not evidence of
frontier difficulty. In PEFT 3083, the adversary's numeric-assertion monkeypatch
attempt failed on an invalid tensor shape before reaching numerical comparison;
its zero reward should not be presented as a universal monkeypatch-resistance
result. Full upstream test suites may also require optional dependencies outside
the packaged task's scoped offline checks.

The target remains 30 tasks. Passing automatic review alone does not add a task to
this count: earlier versions with circular numerical references or missing state
transitions remain held out, with their original evidence and costs preserved.

## What changed after observing failures

**Passing the reference solution is insufficient.** A numerical negative control
using incorrect norms earned full reward in an earlier PEFT task. Tests were
checking agreement between paths through the same submitted computation. The
author and reviewer now require independent expected values and fixtures that
distinguish a plausible wrong formula. Equal weights, zero-initialized parameters
and shape-only checks often hide the behavior a task claims to measure.

**The verifier must accept legitimate alternatives.** Later static audits found
tests that required a list where PEFT also permits a regex, rejected harmless
extra checkpoint files, or recognized only an unsplit matrix projection. These
were concrete false-rejection hypotheses from code inspection, not demonstrated
reward exploits. They triggered separate repairs and additional positive controls.
Each repaired task still needs full validation on its new digest.

**Protect assertions from submitted imports.** A demonstrated pytest monkeypatch
shortcut initially earned reward. After separating the protected pytest process
from the unprivileged interpreter importing the submission, that same shortcut
earned zero and the reference solution earned one. This is evidence against the
demonstrated bypass, not a claim that arbitrary submitted code is proven secure.

**Review the verifier before spending on rollouts.** A focused independent
preflight now reads complete fixtures, observation code, assertions, reference
solution and controls. It asks which assertion rejects specific incorrect
implementations and whether a permitted implementation would fail. A concrete
uncovered contract violation or hidden requirement must block this preflight.
Static findings guide repairs; actual execution and the separate trajectory
review remain mandatory.

**Infrastructure failures need narrow diagnosis.** One pinned CPU model fixture
produced nonfinite pixel gradients on its first backward pass. In a remote A/B
diagnostic, disabling MKLDNN was the only worker change and all eight protected
tests passed. The repair preserved the independent references, tolerances and
finite checks. This diagnoses that fixture/backend combination; it does not
establish a general claim about PyTorch or the underlying model.

**Keep failed attempts and spending attached to the candidate.** Generated Python
cache files once stopped static review as an infrastructure failure. They now
produce author repair feedback. A large review also ran out of reservation
headroom before its final call despite low metered spend; its bounded allowance
was corrected without resetting the candidate or campaign caps. Interrupted
calls, obsolete task variants and their costs remain in the record.

## Evidence and release boundaries

The retained comparison includes per-cell source metadata, runtime hashes,
attempt history, native traces, controls, reviews and costs. The content-addressed
evidence snapshot is private; it should be reviewed and sanitized before any public
release. Raw solver filesystem exports and credentials are excluded from publication.

Automatic admission, independent selection and human review are separate states.
Every released task must identify its exact digest and supporting trials. An
additional audit cannot silently change its files or turn an old passing score
into approval of a new verifier. Tasks continue to carry `human_review: pending`;
agent review is not human certification. The production target of 30 tasks remains
work in progress and is not a result of this pilot.

See [the harness documentation](dynamic_curation.md) for the executable protocol,
budgets, isolation boundary and recovery behavior.
