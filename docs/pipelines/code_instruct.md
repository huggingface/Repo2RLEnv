# `code_instruct`

Magicoder OSS-Instruct, but **grounded in a specific target repo** and
**verified by execution**. The LLM proposes a coding task seeded by a
snippet from the repo's actual code; we run the synthesized test inside
the repo's bootstrap container to confirm the test FAILS without the
oracle and PASSES with it.

| | |
|---|---|
| Status | **shipped (v0.6, hardened v0.8.6)** — Python only |
| Sandbox required at gen | Yes |
| LLM required at gen | Yes (1 call per attempt for problem + test + solution; up to `max_attempts_per_seed`) |
| Reward kinds emitted | `test_execution` |
| Reference dataset | [`AdithyaSK/repo2rlenv-code-instruct`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-code-instruct) — 100 tasks across click, flask, requests, attrs, starlette |
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
    F --> G[Repo-anchoring gate:<br/>solution imports + uses the repo package]
    G --> H[Symbol-collision gate:<br/>task_module names ≠ repo top-level names]
    H --> I[Test-strength gate:<br/>≥3 non-trivial asserts, pytest.raises on error tasks]
    I --> J[Dedup: problem-head + symbol-set fingerprints]
    J --> K[Retry up to max_attempts_per_seed if any gate fails]
    K --> L[Run test alone → must FAIL]
    L --> M[Apply oracle → must PASS]
    M --> N[Emit Harbor task<br/>(instruction bakes the task_module.py delivery contract)]
```

**Quality gates (added v0.8.6)** — the same LLM ships very different tasks under different prompts. Baseline runs produced generic OSS-Instruct problems that ignored the target repo (mean repo-anchoring score 1.4/5). Four post-synthesis gates now enforce anchoring:

- **Repo-anchoring** — AST scan of `task_module.py`: must have `from <repo_pkg> import ...` (or `import <repo_pkg>`) that is actually used.
- **Symbol-collision** — reject candidates whose top-level class/def names collide with an existing repo top-level symbol (blocks the `grep`-and-re-export cheat).
- **Test-strength** — reject weak tests (`assert True`, `<3` non-trivial asserts, missing `pytest.raises` on error-condition instructions).
- **Task dedup** — two independent fingerprints (problem-head + sorted public symbol names); overlap on either counts as duplicate.

When any gate rejects a candidate, the pipeline retries the LLM up to `max_attempts_per_seed` times on the same seed before advancing.

**Delivery contract in the instruction** — the emitted `instruction.md` explicitly tells the solving agent to write its implementation to `/workspace/task_module.py`. Without this, agents (even Sonnet, Codex, and Qwen3.6-35B) write correct logic to naturally-chosen filenames (`ranged_float.py`, `fetcher.py`) and pytest collection fails with `ModuleNotFoundError: No module named 'task_module'`. Wiring this into the emitted instruction bumped Sonnet's solve rate from 40% to 80% at fixed dataset size.

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
| `max_attempts_per_seed` | **3** | retries the LLM when a gate rejects (was `1` in v0.6; bumped in v0.8.6 because the new gates reject more of the first draft) |
| `llm_temperature` | 0.7 | issue + solution |
| `require_test_fails_without_oracle` | `True` | stage A invariant |
| `require_test_passes_with_oracle` | `True` | stage B invariant |
| `skip_decontamination` | `False` | turn off benchmark substring check |
| `skip_validation` | `False` | debug; emits without sandbox run |

## Yield

**Yield = emitted tasks ÷ seed snippets sampled.** With the v0.8.6 gates + retries,
expect **~60–90%** on well-behaved Python libs (empirically, generating the 100-env
reference dataset needed 132 candidates → **75.8% yield** across 5 repos:
click 27→20, flask 24→20, requests 24→20, attrs 23→20, starlette 34→20).

| Knob | Default | Effect on yield |
|---|:-:|---|
| `max_attempts_per_seed` | 3 | ↑ retries a failed synthesis → higher yield, more LLM spend |
| LLM model quality | — | the biggest lever — a stronger model writes verifiable tests more often |
| `seed_min_loc` / `seed_max_loc` | 30 / 200 | very small or very large seeds yield weaker tasks; mid-range is the sweet spot |
| `exclude_glob` | tests/docs/… | keeps seeds anchored to real logic (avoids untestable boilerplate) |
| `skip_decontamination` | False | True keeps would-be-decontaminated seeds (higher yield, contamination risk) |

Common skip reasons in the reference generation run:
- `oracle_does_not_satisfy_test` — LLM's oracle failed its own test in the sandbox (majority of skips; noise inherent to LLM synthesis)
- `duplicate_task` — dedup fingerprint match against an earlier candidate (fires ~2/repo on active seed distributions)
- `missing_pytest_raises` — test-strength gate hit on an error-condition instruction with no `pytest.raises` block

Unlike the `*_runtime` pipelines, repo *test health* doesn't gate yield — the
verifier is self-contained in the emitted task — but the repo still needs to
**bootstrap** (the env runs the generated test). Cost scales with
`limit × max_attempts_per_seed` LLM calls.

## Solve rate on the reference dataset

Sample-validation of the 100-env reference dataset, one task per repo, three agents/backends:

| Agent (Harbor) | Model | Solved | Cost |
|---|---|:-:|---|
| `claude-code` | Sonnet 4.6 (Anthropic direct) | 4/5 | $0.27 |
| `codex` | GPT-5.3-Codex (OpenAI direct) | 4/5 (extrapolated — measured 2/5 before v0.8.6 delivery-contract fix) | $0.28 |
| `openhands-sdk` | Qwen3.6-35B via HF Router | *pending re-run on the fixed dataset* | $0 (Router-hosted) |

Wall clock: ~5 min per trial for `claude-code`, ~4 min for `codex`, ~4 min for `openhands-sdk`.

## End-to-end smoke

```bash
repo2rlenv generate \
  --repo pallets/click \
  --pipeline code_instruct \
  --pipeline-opt limit=1 \
  --pipeline-opt seed=42 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./workspace/datasets/click-cinst

# Oracle: should score 1.0
harbor run -p ./workspace/datasets/click-cinst -a oracle -l 1 -n 1 -k 1 -y

# Real agent: Sonnet via claude-code
harbor run -p ./workspace/datasets/click-cinst \
  -a claude-code -m anthropic/claude-sonnet-4-6 \
  -l 1 -n 1 -k 1 -y

# OpenHands + HF Router (any router-hosted model)
harbor run -p ./workspace/datasets/click-cinst \
  -a openhands-sdk -m "openai/Qwen/Qwen3.6-35B-A3B:scaleway" \
  --ae "LLM_BASE_URL=https://router.huggingface.co/v1" \
  --ae "LLM_API_KEY=$HF_TOKEN" \
  -l 1 -n 1 -k 1 -y
```

Or pull the published 100-env dataset directly:

```bash
repo2rlenv pull AdithyaSK/repo2rlenv-code-instruct ./workspace/datasets/code-instruct
harbor run -p ./workspace/datasets/code-instruct -a oracle -l 5 -n 4 -k 1 -y
```

## What we adapted from `references/magicoder/`

- Seed-snippet → instruction recipe (`src/magicoder/generate_data.py:79-84`)
- Section-marker output format (`data/prompt.txt`)
- Section-by-section parsing (`src/magicoder/generate_data.py:87-102`)
- Substring-based decontamination (`decontamination/find_substrings.py`)

No code is copied. The execution-verification layer is original.
