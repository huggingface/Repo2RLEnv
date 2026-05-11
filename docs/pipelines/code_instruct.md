# `code_instruct`

Magicoder OSS-Instruct, but **grounded in a specific target repo** and
**verified by execution**. The LLM proposes a coding task seeded by a
snippet from the repo's actual code; we run the synthesized test inside
the repo's bootstrap container to confirm the test FAILS without the
oracle and PASSES with it.

| | |
|---|---|
| Status | **shipped (v0.6)** — Python only |
| Sandbox required at gen | Yes |
| LLM required at gen | Yes (single call for problem + test + solution) |
| Reward kinds emitted | `test_execution` |
| Inspiration | [Magicoder](https://github.com/ise-uiuc/magicoder) (ICML '24) |

## What's different vs vanilla OSS-Instruct

Magicoder samples seeds from a global OSS corpus (~150K files) and emits
text-only `(problem, solution)` pairs. Repo2RLEnv's variant differs in
three ways:

1. **Seeds come from one target repo** — the synthesized task is solvable
   in *that* repo's environment.
2. **Each task ships an executable verifier** (a pytest test) the LLM
   also writes — not just prose.
3. **The oracle must actually pass the test** in the repo's Docker env.

That third invariant is the load-bearing contribution. Magicoder doesn't
do it; nobody currently does it for arbitrary repos.

## Algorithm

```mermaid
flowchart TD
    A[Repo URL] --> B[bootstrap: build env at HEAD]
    B --> C[Sample random Python file +<br/>30–200-line window]
    C --> D[Filter: skip mostly-boring blocks]
    D --> E[ONE LLM call:<br/>Problem + Test + Solution]
    E --> F[Decontaminate vs known benchmarks]
    F --> G[Syntactic: test imports from task_module?]
    G --> H[Run test alone → must FAIL]
    H --> I[Apply oracle → must PASS]
    I --> J[Emit Harbor task<br/>(adds task_module.py + test_r2e_<hash>.py)]
```

## Pipeline shape (emitted task)

```
<owner>__<repo>-cinst-<hash>/
├── task.toml                 # name = "<org>/<slug>"
├── instruction.md            # LLM-authored problem statement
├── environment/Dockerfile    # FROM bootstrap; HEAD state
├── tests/test.sh             # `python -m pytest test_r2e_<hash>.py -v`
└── solution/
    ├── patch.diff            # adds task_module.py + test file at repo root
    └── solve.sh              # `git apply patch.diff` shim
```

The gold patch.diff adds **two new files**: `task_module.py` (the oracle)
and `test_r2e_<hash>.py` (the verifier). The agent's job is to make
`task_module.py` satisfy the test.

## Prompt + parsing

One call asks the LLM for three sections in fixed order:

```
[Problem Description]
<self-contained problem statement>

[Test]
<pytest test that imports from `task_module`>

[Solution]
<the `task_module.py` content>
```

`parse_task_response` extracts the three blocks via case-insensitive
marker scanning; markdown code fences are stripped.

## Verification invariants

We run two stages inside the bootstrap container:

| Stage | What runs | Required outcome |
|---|---|---|
| A — test only | write test file; `pytest <test>` | FAIL (else the test is trivial) |
| B — test + oracle | write both files; `pytest <test>` | PASS (else the oracle is wrong) |

If either invariant breaks, the task is skipped. Both stages clean up
after themselves so the next candidate starts from a clean tree.

## Options

See `CodeInstructOptions` in `src/repo2rlenv/spec/options.py`. Key fields:

| Field | Default | Notes |
|---|---|---|
| `limit` | 50 | max emitted tasks |
| `seed_min_loc` / `seed_max_loc` | 30 / 200 | snippet window size |
| `file_glob` / `exclude_glob` | `**/*.py` / tests/etc. | seed source selection |
| `llm_temperature` | 0.7 | issue + solution |
| `require_test_fails_without_oracle` | `True` | stage A invariant |
| `require_test_passes_with_oracle` | `True` | stage B invariant |
| `skip_decontamination` | `False` | turn off benchmark substring check |
| `skip_validation` | `False` | debug; emits without sandbox run |

## End-to-end smoke

```bash
repo2rlenv generate \
  --repo pallets/click \
  --pipeline code_instruct \
  --pipeline-opt limit=1 \
  --pipeline-opt seed=42 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./datasets/click-cinst

harbor run -a oracle -p ./datasets/click-cinst/<task-id>
# Mean reward 1.000
```

## What we adapted from `references/magicoder/`

- Seed-snippet → instruction recipe (`src/magicoder/generate_data.py:79-84`)
- Section-marker output format (`data/prompt.txt`)
- Section-by-section parsing (`src/magicoder/generate_data.py:87-102`)
- Substring-based decontamination (`decontamination/find_substrings.py`)

No code is copied. The execution-verification layer is original.
