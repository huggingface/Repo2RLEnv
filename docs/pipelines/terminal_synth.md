# `terminal_synth` — owned terminal task generation

The first implemented recipe is **`seta_seed2synth`**, adapted from
[SETA](https://github.com/camel-ai/seta/tree/e4715b01174e6c9503fc46120d81dd692ced75e6).
It preserves the released seed-to-idea and test-first datapoint-building stages.
The controller writes structured author outputs as Harbor tasks, executes a fresh
baseline and reference, and returns failures to the builder for bounded repair.

```mermaid
flowchart LR
  S[Seed records] --> C[Extract capabilities]
  C --> D[Task design and private builder notes]
  D --> B[Tests, fixtures, reference and instruction]
  B --> H[Harbor bundle]
  H --> E[Remote baseline and reference]
  E -->|Failure details| B
  E -->|Baseline 0, reference 1| O[Generated task and evidence]
```

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
