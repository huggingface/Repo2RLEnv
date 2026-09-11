# `terminal_synth` — owned terminal task generation

The first implemented recipe is **`seta_seed2synth`**, adapted from
[SETA](https://github.com/camel-ai/seta/tree/e4715b01174e6c9503fc46120d81dd692ced75e6).
It preserves the released seed-to-idea and test-first datapoint-building stages.
The controller writes structured author outputs as Harbor tasks, executes a fresh
baseline and reference, and returns failures to the builder for bounded repair.

## Pipeline, step by step

```mermaid
flowchart TD
  S["JSON / JSONL question and optional answer"] --> D["Deduplicate and order seeds"]
  D --> P1["P1 · Extract capabilities and draft task design"]
  P1 --> P2["P2 · Author tests, fixtures, instruction and reference"]
  P2 --> V["Validate TerminalDraft; write temporary Harbor bundle"]
  V --> R["Remote fresh nop + oracle trials"]
  V -->|"Schema error"| F["Previous draft + concrete failure feedback"]
  R -->|"Wrong reward or execution failure"| F
  F -->|"At most max_repairs"| P2
  R -->|"nop 0; oracle 1"| H["Export Harbor task and input lineage"]
```

`P1`, `P2`, … identify actual model calls. Unlabelled stages are code or remote execution.

**Choose the skill.** A seed is evidence about a real workflow. One design call extracts core_capabilities and a draft_spec; capability extraction and task design are not separate API calls.

**Materialize the task.** The builder returns all files through TerminalDraft. It is told to reason about tests first; the controller does not run a separate test-author call for this recipe.

**Execute and repair.** The controller owns Dockerfile scaffolding, test contracts and reward code. It rebuilds the task remotely and returns actual schema, baseline or oracle failures to the same builder.

## Every prompt and its data

Two calls on the first successful attempt: design, then builder. Each repair repeats only the builder.

| Call | System prompt composition | User / input material | Output | Retry or branch |
|---|---|---|---|---|
| P1 · Design | idea_prompt.md + owned adaptation | Full seed JSON: question, optional answer, source metadata. | TaskDesign: core_capabilities, draft_spec | Invalid schema skips this seed. |
| P2 · Build / repair | builder_prompt.md + shared materialization instructions | Design and accumulated feedback, including the previous draft after a failure. | TerminalDraft | Initial call plus max_repairs retries; default three builder attempts. |

Read the [complete seta_seed2synth prompt reference](prompts/seta_seed2synth.md) for every retained template, appended instruction, substitution, example and output schema. The [shared prompt guide](prompt_reference.md) explains how to inspect the fully resolved request from a real run.

## Follow one task

Illustration: a question about filenames containing spaces becomes a directory-processing task with awkward filenames, a precise output contract, a shell reference and tests that inspect the resulting files. The seed answer informs design but is not automatically copied into the learner instruction.

## What repeats, what is checked

A completed pair must return exactly 0 for the unsolved task and 1 for the reference. A model self_review is a consistency note, not proof that either execution passed. Parent/source metadata and model requests remain separate from learner-visible files.

An exported bundle is a generation result. Independent leakage review, shortcut probes and blind solver traces belong to the later quality campaign.

## Implementation map

- [`seta_seed2synth/recipe.py`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/seta_seed2synth/recipe.py)
- [`terminal/runner.py`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/terminal/runner.py)
- [`terminal/draft.py`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/terminal/draft.py)
- [`terminal/grade.py`](https://github.com/huggingface/Repo2RLEnv/blob/codex/owned-generation-pipelines/src/repo2rlenv/pipelines/recipes/terminal/grade.py)



## Input and CLI

Supply a JSON array or JSONL of seed objects. Each requires `source`, `title` and
`question_text`; include `answer_text`, `tags`, `url`, attribution and license
metadata when available. Exact duplicate records are deduplicated before model
calls. Seed files are input data, not an upstream package dependency.

```json
{"source":"unix_linux_se","title":"Preserve filenames with spaces","question_text":"A batch script splits paths containing spaces. How can its traversal preserve complete filenames?","answer_text":"Use a delimiter that cannot occur in filenames and keep expansions quoted."}
```

Create a campaign and remote worker as described in [owned recipes](owned_recipes.md),
build the owned runtime with `uv build`, then run:

```bash
repo2rlenv generate --config examples/owned-seta.yaml
repo2rlenv generate --config examples/owned-seta.yaml --resume --json
```

| Option | Default | Meaning |
|---|---:|---|
| `target` | 20 | Number of execution-verified generated tasks |
| `max_candidates` | 40 | Maximum distinct input records to try |
| `max_repairs` | 2 | Builder repairs after the first attempt |
| `seed` | 24 | Deterministic ordering of the input records |
| `max_tokens` | 10000 | Maximum output tokens per builder call |
| `test_timeout_sec` | 120 | Time limit for the generated tests |

Run receipts retain source identity, model requests, task designs, each materialized
attempt, author self-review and actual Harbor trial results. A completed export is
reused only when its content matches the recorded identity. Ambiguous remote
outcomes stop dispatch; they are not silently retried.

## Supported profile and adaptations

The current profile is a Linux CPU container based on Python 3.12, with bash, jq,
sqlite3, git, curl, tmux, uv and pytest. Tasks may install additional dependencies
during image build. Execution is offline. Systemd, privileged networking, GPUs and
external services are outside this profile. Verifiers inspect the final container
state; reference and test files are excluded from the learner image build context.

The upstream idea and datapoint prompt files are retained with their Apache-2.0
license. Owned runtime additions replace folder/tool output with strict JSON,
legacy Harbor metadata with schema 1.3, and network-installing test scripts with
preinstalled dependencies and deterministic JUnit parsing. Five to ten weighted
tests and author self-review remain part of the method; reward is binary. This is
a documented workflow adaptation, not byte-identical upstream execution.

The current milestone is 20 generated tasks per method. Specification audits,
adversarial verifier checks and blind solver evaluations follow generation. A
passing author self-review is not independent quality acceptance.

See [RFC 0013](../rfcs/0013-seta-seed2synth-recipe.md) and the packaged
`pipelines/recipes/seta_seed2synth/provenance.md` for source mapping and notices.
