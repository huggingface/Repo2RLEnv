# Pipeline RFCs

Design docs for new synthesis pipelines. One RFC per pipeline. Written **before** the code lands; kept in the repo after the pipeline ships as a permanent record of *why* the pipeline exists in the shape it does.

## Why RFCs

A pipeline is 100–300 LOC of code, but the decisions behind it — repo shape it fits, verification approach, contamination story, LLM use, yield expectations — are much harder to reconstruct from a merged diff months later. Every new pipeline in this repo has had non-obvious design choices (issue-fetch fallback in `commit_runtime`, PoC-agent for `cve_patches`, anti-contamination compose overlay, graded vs. binary reward). Those choices deserve a durable home separate from the implementation.

An RFC also front-loads the audit: writing the "how does contamination get in?" section forces you to think about it *before* you've shipped 100 published envs.

## When to write one

- **Always** for a new pipeline (new entry in `PipelineName`).
- **Optional** for meaningful reshapes of an existing pipeline (e.g. adding LLM synthesis to `commit_runtime` in v0.8.4 — retrospectively RFC-worthy).
- **Skip** for polish / bug-fix work that fits in a PR description.

**Retrospective RFCs.** Pipelines that shipped before this process existed (RFCs 0001–0006) have RFCs written *after* their initial merge, as archival records. They're `status: implemented` from day one and their [Implementation](#lifecycle) sections link back to the initial PR, source file, doc page, and reference dataset. Retro RFCs are lighter on the "alternatives considered" front (memory decays) and heavier on cross-referencing the current authoritative doc (`docs/pipelines/<name>.md`) as the source of truth. Do not write retro RFCs for pipelines that have been withdrawn (`mutation_bugs`, `refactor_synthesis`) — git history is enough.

## Process

1. **Pick a candidate** from [`plans/candidate_pipelines.md`](https://github.com/huggingface/Repo2RLEnv/blob/main/plans/candidate_pipelines.md), or propose a new one.
2. **Copy [`TEMPLATE.md`](./TEMPLATE.md)** to `docs/rfcs/NNNN-<name>.md` (next unused 4-digit number, kebab-case name).
3. **Fill in every section** — write "n/a" if a section genuinely doesn't apply, don't just delete it. If you don't know an answer yet, mark it `TBD` and open it in "Open questions."
4. **PR the RFC alone** first — reviews want to look at the design without the implementation blur. RFCs at this stage should carry the label `rfc:draft` (add it via `gh pr edit --add-label rfc:draft`).
5. **Iterate on the RFC** based on review. Update the status header as it moves through the lifecycle (see below).
6. **Once accepted**, implement the pipeline in a follow-up PR that references the RFC number and follows [`docs/contributing/ADDING_A_PIPELINE.md`](../contributing/ADDING_A_PIPELINE.md).
7. **After merge**, update the RFC's status to `implemented`, add the merge commit + PR link, and link the reference dataset from the "Rollout" section.

## Lifecycle

RFCs have a `Status:` header line that moves through these states:

- `draft` — being written; not yet ready for design review.
- `review` — ready for feedback; PR open.
- `accepted` — design signed off; implementation can begin.
- `implemented` — code has landed on `main`. RFC is now archival.
- `withdrawn` — decided against. Kept for posterity; explain why in a final "Withdrawal" section.
- `superseded` — replaced by a later RFC (link both directions).

## Numbering

Sequential. `0001-<name>.md`, `0002-<name>.md`, …. Never reuse a number. If an RFC is withdrawn, the number is retired with it.

## Index

| # | Pipeline | Status | RFC | Reference dataset |
|---|---|---|---|---|
| 0001 | `pr_diff` | implemented (stable) | [0001-pr-diff.md](./0001-pr-diff.md) | [`repo2rlenv-pr-diff`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff) (181) |
| 0002 | `pr_runtime` | implemented (stable) | [0002-pr-runtime.md](./0002-pr-runtime.md) | [`repo2rlenv-pr-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime) (100) |
| 0003 | `commit_runtime` | implemented (stable) | [0003-commit-runtime.md](./0003-commit-runtime.md) | [`…commit-runtime-v2`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime-v2) (100) |
| 0004 | `code_instruct` | implemented (experimental) | [0004-code-instruct.md](./0004-code-instruct.md) | — |
| 0005 | `equivalence_tests` | implemented (experimental) | [0005-equivalence-tests.md](./0005-equivalence-tests.md) | — |
| 0006 | `cve_patches` | implemented (experimental) | [0006-cve-patches.md](./0006-cve-patches.md) | [`…cve-patches`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-cve-patches) (19) |
| 0007 | `pr_to_env` | draft | [0007-pr-to-env.md](./0007-pr-to-env.md) | — |
| 0008 | `env_setup` | draft | [0008-env-setup.md](./0008-env-setup.md) | — |
| 0009 | `test_synthesis` | draft | [0009-test-synthesis.md](./0009-test-synthesis.md) | — |
| 0010 | `issue_runtime` | draft | [0010-issue-runtime.md](./0010-issue-runtime.md) | — |

<!-- Update this table whenever a new RFC lands or an RFC's status changes. -->

## Related

- [`plans/candidate_pipelines.md`](https://github.com/huggingface/Repo2RLEnv/blob/main/plans/candidate_pipelines.md) — the backlog. Ranking + inspirations. RFCs are drawn from here (or a fresh proposal).
- [`docs/contributing/ADDING_A_PIPELINE.md`](../contributing/ADDING_A_PIPELINE.md) — the implementation cookbook. RFC covers the *why*; the cookbook covers the *how*.
- [`docs/reference/RELATED_WORK.md`](../reference/RELATED_WORK.md) — provenance table for shipped pipelines. Add an entry here when an RFC ships.
