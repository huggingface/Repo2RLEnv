# DataArc-inspired terminal synthesis

Method credit: [DataArc-SynData-Toolkit](https://github.com/DataArcTech/DataArc-SynData-Toolkit),
terminal branch `2a1d65ec8dcfaea2458d67e1fb18078cce6420b9`, specifically
`sdgsystem/agentic_data/terminal_bench.py`. Its seed augmentation method enumerates
few-shot, self-instruct, breadth evolution and depth evolution variants before
direct artifact authoring.

## Source and licensing boundary

Recipe version **2** uses Repo2RLEnv-authored prompt and strategy wording under
this repository's Apache-2.0 license. No source code or prompt text from the
terminal branch is retained. The Python implementation uses our seed loader,
typed draft, metered model client and shared Harbor runner. Strategy identifiers,
file selection, the 3,500-character excerpt limit and marker filtering describe
the method we implement; they are not an assertion of a license on upstream text.

Version 1 retained upstream prompts and an Apache license from main at
`cc7ea8f8c16bd2d39a2e4a790cc24b2b370ce1bb`. The terminal revision has no recorded
license grant, and that main revision does not contain the terminal implementation.
The copied prompts and unrelated license file have therefore been removed from
the current package. The catalog records `NOASSERTION` for the referenced upstream
revision and does not infer coverage from another branch. Existing version 1
datasets and archived runs are historical evidence; this change does not revise
their artifacts or establish their licensing or quality status.

## Implemented profile

Each seed produces independent variants, not a chained curriculum. There is no
separate design model call. Instructions, fixtures, reference code and private
tests are authored as a TerminalDraft. Full text environment context supplements
bounded seed excerpts. Parent hashes identify the original bundle before marker
filtering. A fresh offline Harbor baseline/reference pair and bounded repairs
follow static materialization. Dependencies are installed during image build.

The profile supports one CPU container and text fixtures. Models are explicitly
configured. Repairs preserve the selected domain, tools and goal. Version 2
outputs carry `recipe_version = "2"`; runtime-wheel hashes prevent an old run
from silently resuming with changed prompt resources. Seed data are supplied by
the caller and retain their own licensing requirements. No upstream project is
imported, installed or cloned at runtime. Exports are generation artifacts,
not independent quality acceptance or training approval.
