---
title: "Maintain the documentation"
navTitle: "Maintain documentation"
description: "How the docs site is built, where content lives, and the rules for changing it."
---

Write for someone using or extending Repo2RLEnv. Describe the implemented behavior,
its limits and a runnable example. Put campaign diaries, provider receipts, budget
approvals and one-off input selections in ignored `workspace/` directories.

## How the site is built

The site is a [Fumadocs](https://fumadocs.dev) app (Next.js, static export) in
`website/`. Content stays in `docs/`, so every page is also readable on GitHub and
paths such as `docs/pipelines/pr_runtime.md` stay stable — published dataset cards
link to them.

```files
docs
├── meta.json            # sidebar: sections, order and cross-folder pages
├── quickstart.mdx       # .mdx pages may use components (Steps, Cards, Tabs, Film)
├── pipelines
│   ├── meta.json
│   └── pr_runtime.md    # .md pages are plain Markdown (keep these paths stable)
└── _tools               # generators (not pages)
website
├── app                  # routes: landing page, docs pages, search, llms.txt
├── components           # Film, Mermaid, link resolution
└── tools/check-links.mjs
```

## Build and preview

Requires Node.js 22+ and Python 3.12+. No model keys, cloud accounts or private
campaign directories are needed; the build reads source files as text and never
executes task code.

```bash
cd website
npm ci
npm run dev                  # http://localhost:3000, hot reload
npm run build                # static site in website/out
node tools/check-links.mjs   # every internal link and #anchor must resolve
```

`npm run dev` and `npm run build` first regenerate `docs/pipelines/prompts/` from
canonical templates and request-assembly code. That directory is ignored; edit the
canonical prompt or `docs/_tools/generate_prompt_reference.py`, never the generated
pages.

## Write a page

Every page starts with front matter; the site renders the title, so don't repeat it
as a `#` heading:

```yaml
---
title: "Run tasks with Harbor"
description: "One sentence that says what this page gets you."
film: pr-runtime   # optional: a live explainer film above the body
---
```

- Add the page to a `meta.json` (usually `docs/meta.json`) or it won't appear in the
  sidebar. Root entries may point into other folders (`"pipelines/quality_loop"`).
- Link with relative file paths (`../concepts/tasks.mdx`, `pr_runtime.md#options`);
  links to non-page files open on GitHub.
- In `.md` files use Markdown only: GitHub alerts (`> [!NOTE]`), ```` ```files ````
  trees, ```` ```mermaid ```` diagrams and `tab="…"` code tabs all render on the site.
- In `.mdx` files you can also use `<Steps>`, `<Cards>`, `<Tabs>`, `<Callout>` and
  `<Film id="…" />`. Escape `<` and `{` in prose.

Each pipeline guide should explain its inputs, show one stage diagram, map model
calls to their inputs and outputs, and describe verification and bounded repair.
Link to canonical prompts, upstream credits, measured economics and the dataset.
Keep shared execution and quality contracts in their common guides and RFCs.

## Update measured results

Review the evidence and update `docs/data/pipelines.json`, then run:

```bash
python3 docs/_tools/generate_metrics.py
python3 docs/_tools/generate_metrics.py --check
```

Keep only sanitized aggregate measurements and public dataset revisions in this
file. Count retries under their candidate identity, separate retained tasks, and
keep generation, quality evaluation and solver outcomes distinct. Costs must
name their sample and include failures; unavailable compute or yield is not zero.
Keep raw evidence locally or with an appropriate dataset/release artifact.

Historical native-pipeline records live in the same file under `native_history`.
Preserve their evidence scope: a cached inventory, a generation-time verification
stamp and a Harbor oracle gate are different observations. Do not carry a gate
from an older cohort onto a newer dataset. The old synthesis counters are
run-cumulative; sum one final counter per run, not every exported task's counter.
Update [native results](../pipelines/native_results.md) when changing those
measurements, and retain source hashes or pinned public manifests.

## Explainer films

The films on pipeline pages are live [Remotion](https://www.remotion.dev) compositions
from the hf-motion repository, played in the browser so they follow the site's light
and dark themes. They are vendored as one module in `website/vendor/films/`; rebuild
and copy it from hf-motion when a film changes.

## Review changes

Check the stage diagram against the implementation, validate example configs and
build the site from a clean checkout. CI regenerates the prompt reference, checks the
metrics tables, builds the site, checks every internal link and anchor, and fails if
the build changed tracked files. Inspect changed diagrams in a browser, in light and
dark mode. Preserve upstream notices and source pins when condensing a guide or RFC.
