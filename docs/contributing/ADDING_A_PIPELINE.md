---
title: "Cookbook: adding a new pipeline"
navTitle: "Add a pipeline"
---

This page walks you through shipping a new synthesis pipeline. Keep [`pr_diff`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/pipelines/pr_diff.py) open as you go: it's the reference implementation.

> **Before you code an entirely new pipeline**, write an RFC in [`docs/rfcs/`](../rfcs/index.md). The RFC explains *why* the pipeline has the shape it does: its motivation, verification approach, contamination story, LLM use and expected yield. It lets people review the design before there's code to argue about. Small reshapes of an existing pipeline don't need one; a new `PipelineName` entry always does. Copy [`docs/rfcs/TEMPLATE.md`](../rfcs/TEMPLATE.md) to `docs/rfcs/NNNN-<name>.md`, land the RFC, then follow this cookbook.

## What you're building

A class that:

- Implements the [`Pipeline` Protocol](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/pipelines/base.py)
- Validates its kwargs through a Pydantic Options model
- Reads from a repo (using `gh` and/or `git`) and/or an LLM (via LiteLLM)
- Optionally builds Docker envs (via Harbor for full pipelines)
- Emits Harbor task directories at `out_dir`
- Returns a `PipelineResult` with candidate / emitted / skipped counters

Most pipelines come in at 100–300 lines. Lite (text-only) pipelines sit at the low end, and sandbox-required ones with LLM verification at the high end.

## Prerequisites

- A name for your pipeline (lowercase snake_case, e.g. `commit_runtime`)
- A clear answer to: does it need a sandbox at generation time? Does it need an LLM?
- A reference repo or paper you're drawing inspiration from, cloned to `references/<name>/`

## Step-by-step

### 1. Add the enum value

Edit [`src/repo2rlenv/spec/input.py`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/spec/input.py):

```python
class PipelineName(StrEnum):
    PR_DIFF = "pr_diff"
    PR_RUNTIME = "pr_runtime"
    YOUR_PIPELINE = "your_pipeline"          # ← add this
```

### 2. Create the Options model

Edit [`src/repo2rlenv/spec/options.py`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/spec/options.py). All Options classes inherit from `_BaseOptions` (which sets `extra="forbid"` so unknown keys raise):

```python
class YourPipelineOptions(_BaseOptions):
    """Validated kwargs for your_pipeline."""

    limit: int = 100
    since: date | None = None
    # ... pipeline-specific fields with defaults
    # use Literal types where possible — they round-trip cleanly through CLI

# Register at the bottom of the file
OPTIONS_REGISTRY: dict[str, type[_BaseOptions]] = {
    "pr_runtime": PRRuntimeOptions,
    "pr_diff": PRDiffOptions,
    "your_pipeline": YourPipelineOptions,    # ← add this
}
```

**Conventions** (lifted from `pr_diff`):

- `limit: int`, defaulting to 50–100. Over-fetch ~3× internally so filtering doesn't shrink the batch to zero
- Date filters: `since: date | None` and `until: date | None`
- Booleans default to the safer choice (e.g. `skip_drafts: bool = True`)
- Caps to avoid pathological inputs (e.g. `max_files_per_pr: int = 5`)

### 3. Implement the pipeline

Create `src/repo2rlenv/pipelines/your_pipeline.py`. Skeleton:

```python
"""<one-line description of what this pipeline does>.

Acknowledgment block — required if you're drawing on external work.
See pr_diff.py for the format.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_github_token
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import YourPipelineOptions

logger = logging.getLogger(__name__)


class YourPipeline:
    """One-line summary. Implements the `Pipeline` Protocol."""

    name: ClassVar[PipelineName] = PipelineName.YOUR_PIPELINE

    def __init__(self, input: GenerationInput, options: YourPipelineOptions):
        self.input = input
        self.options = options

    def run(self, out_dir: Path) -> PipelineResult:
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1) Resolve auth if you need to clone/list private repos
        token = resolve_github_token(self.input.repo, self.input.auth)
        if self.input.repo.access == "private" and not token:
            raise RuntimeError(
                "private repo specified but no GitHub token resolved. "
                "Run `gh auth login` or set GITHUB_TOKEN."
            )

        # 2) Discover candidates (the pipeline-specific logic)
        candidates = self._discover()

        # 3) Loop, filter, emit
        skip_reasons: dict[str, int] = {}
        emitted = 0
        for cand in candidates:
            reason = self._should_skip(cand)
            if reason:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                continue
            try:
                task = self._build_task(cand)
                write_harbor_task(task, out_dir)
                emitted += 1
            except Exception as exc:
                logger.warning("candidate %s failed: %s", cand, exc)
                skip_reasons["build_failed"] = skip_reasons.get("build_failed", 0) + 1

        return PipelineResult(
            candidates=len(candidates),
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # --- private helpers ---

    def _discover(self) -> list[...]:
        """Pipeline-specific: list PRs, walk commits, query NVD, sample seeds, etc."""
        ...

    def _should_skip(self, cand) -> str | None:
        """Return a skip reason name (or None to keep). Reasons go in the result."""
        ...

    def _build_task(self, cand) -> HarborTask:
        """Construct the Harbor task — see HarborTask for required fields."""
        owner, name = self.input.repo.owner_name
        task_id = f"{owner}__{name}-{cand.id}"
        repo2env = {
            "pipeline": self.name.value,
            "pipeline_version": "0.1.0",
            "repo": f"{owner}/{name}",
            "ref": cand.base_sha,
            "reference": cand.url,
            "source_access": self.input.repo.access,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "synthesis_llm": self.input.llm.qualified_name,
            self.name.value: {
                # Pipeline-specific provenance under [metadata.repo2env.<name>]
                ...
            },
        }
        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=...,
            instruction=...,
            oracle_diff=...,
            repo2env=repo2env,
        )
```

**Key invariants** (the contract test enforces these):

- `name: ClassVar[PipelineName] = PipelineName.YOUR_PIPELINE`, typed and matching the enum value
- `__init__(input, options)` accepts the GenerationInput + your specific Options
- `run(out_dir)` returns a `PipelineResult`
- Per-task failures go into `skip_reasons` and **don't halt the pipeline**

### 4. Wire helpers you need

Reuse what already exists:

| Need | Module |
|---|---|
| GitHub PR list / diff | `repo2rlenv.github` (`list_merged_prs`, `fetch_pr_diff`) |
| Token resolution (`gh`, env, etc.) | `repo2rlenv.auth.resolve_github_token` |
| LLM call (any provider via LiteLLM) | `repo2rlenv.llm.complete(spec, system, user, ...)` |
| Diff-similarity reward computation | `repo2rlenv.reward.calculate_diff_similarity_reward` |
| Writing Harbor task dirs | `repo2rlenv.emitter.harbor.write_harbor_task` |

### 5. Register the pipeline

Edit [`src/repo2rlenv/pipelines/__init__.py`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/pipelines/__init__.py):

```python
from repo2rlenv.pipelines.your_pipeline import YourPipeline

PIPELINES: dict[str, type[Pipeline]] = {
    "pr_diff": PRDiffPipeline,
    "your_pipeline": YourPipeline,           # ← add this
}
```

### 6. Test it

#### Unit test (mandatory)

Add `tests/test_your_pipeline.py` with at least:

```python
def test_your_pipeline_options_strict():
    """Unknown fields should raise."""
    from repo2rlenv.spec.options import YourPipelineOptions
    with pytest.raises(Exception):
        YourPipelineOptions(unknown_field=42)
```

The contract test (`tests/test_pipeline_contract.py`) picks up your new entry automatically. If you missed a conformance step, it fails there with a clear message.

#### Live e2e test (recommended)

Mirror `tests/test_e2e_public.py`: point the pipeline at a small public repo, assert that at least one task is emitted, and confirm the oracle round-trips through the diff-similarity reward (oracle vs oracle = 1.0):

```python
@pytest.mark.skipif(not _gh_authenticated(), reason="gh not authenticated")
def test_e2e_your_pipeline(tmp_path: Path):
    gen_input = GenerationInput(
        repo=RepoSpec(url="huggingface/trl", access="public"),
        pipeline=PipelineSpec(name=PipelineName.YOUR_PIPELINE, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination=str(tmp_path), org="x", dataset_name="y"),
    )
    pipeline = YourPipeline(gen_input, YourPipelineOptions(limit=2))
    result = pipeline.run(tmp_path)
    assert result.emitted >= 1
```

#### Smoke run

```bash
uv run repo2rlenv generate \
  --repo huggingface/trl \
  --pipeline your_pipeline \
  --pipeline-opt limit=2 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./out_test
```

### 7. Write the doc

Add `docs/pipelines/your_pipeline.md`. Mirror the structure of [`pr_diff.md`](../pipelines/pr_diff.md):

- A 1-row metadata table (status, sandbox required, LLM required, reward kinds, inspiration, reference clone, implementation file, options model)
- A Mermaid `flowchart TD` of the algorithm, with the skip and fail edges as well as the happy path
- An "Options" section with the Pydantic class signature and a per-field table
- The `[metadata.repo2env.<name>]` schema
- A table of the skip reasons your pipeline emits
- Example CLI and Python invocations
- A "Limitations" section

Then add an entry for your pipeline to `website/lib/catalog.ts` (it drives the [pipeline catalogue](../pipelines/index.mdx), the landing page and the summary strip on your page), add the page under its task-type group in `docs/pipelines/meta.json` so it appears in the sidebar, and, if it introduces a new reward kind, describe it in [`docs/concepts/rewards.mdx`](../concepts/rewards.mdx).

### 8. (Optional) acknowledgments

If your pipeline draws code or algorithms from external work, add an "Acknowledgment" block at the top of your `.py` file in the format `reward.py` and `pr_diff.py` use. Be explicit about:

- Which paper/repo inspired the approach
- Their license
- Whether you copied code or independently reimplemented
- Why the upstream license does or doesn't bind your file

## Mental model

A pipeline is a generator over candidates. Filters and optional QA gate each one, and the survivors become Harbor task directories:

```
discover() ─▶ filter ─▶ build_task() ─▶ (optional QA) ─▶ write_harbor_task()
              │                                         │
              ▼                                         ▼
        skip_reasons                               PipelineResult
```

You can test each stage on its own. `_discover` should do network IO only, `_should_skip` should be a deterministic predicate, and `_build_task` should have no side effects. `write_harbor_task` is the only filesystem write.

## Common patterns from `pr_diff`

- **Over-fetch, then filter client-side.** Run `gh pr list --limit (limit*3)`, then trim after applying `since`/`until`/`max_files_per_pr`. GitHub's API doesn't always honor compound filters cleanly.
- **Keep task IDs stable**, as in `<owner>__<repo>-<number>`. The same input gives the same output, so re-runs are idempotent.
- **Strip `Closes #N` boilerplate** when you build instructions from PR bodies. It leaks the answer.
- **Don't log secrets.** Never put a token in an exception message or a log line. The token-injection helper in `auth.auth_clone_url` exists so you never have to handle one yourself.

## Failure modes to design for

| Situation | What to do |
|---|---|
| A network blip while fetching one candidate | Log it, add it to `skip_reasons` and continue |
| A network failure during the *initial* repo discovery (for example, a `gh pr list` 401) | Raise. The pipeline can't proceed |
| No results (no PRs match the filters) | Return `PipelineResult(emitted=0)` rather than raising. The CLI exits 1 when `emitted == 0` |
| The output directory already holds tasks | Overwrite them. `write_harbor_task` is idempotent in practice |

## Reward-kind decision

Every pipeline declares which reward kinds its tasks support. Set this in the `repo2env` metadata when constructing the task:

| Kind | Set when... |
|---|---|
| `diff_similarity` | You ship a `solution/patch.diff` that's compared via sequence similarity |
| `test_execution` | You ship `environment/Dockerfile` + `tests/test.sh` that produce a binary reward |

A task may emit both. Lite pipelines emit only `diff_similarity`; full, sandbox-required pipelines usually emit both.

If you don't override it, `write_harbor_task` fills in `reward_kinds = ["diff_similarity"]`. Set `repo2env["reward_kinds"]` explicitly when you ship something else.

## Submitting

Before you open a PR, check that:

1. `uv run pytest -q` passes, including the contract test that picks up your new entry.
2. The doc page exists and the pipeline catalogue lists it.
3. The pipeline runs end to end against a real public repo.
4. If you target private repos, the e2e test against a private repo also passes locally.

Then commit and open a PR. The contract test is your safety net: if it passes, the pipeline is structurally sound, and review can focus on the synthesis logic.
