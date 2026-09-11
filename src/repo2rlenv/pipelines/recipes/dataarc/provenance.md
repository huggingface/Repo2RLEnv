# DataArc terminal synthesis adaptation

Source: [DataArc-SynData-Toolkit](https://github.com/DataArcTech/DataArc-SynData-Toolkit),
terminal branch commit `2a1d65ec8dcfaea2458d67e1fb18078cce6420b9`.
`artifact_prompt.md` and `strategies.json` retain the authoring prompt and four
strategy texts from `sdgsystem/agentic_data/terminal_bench.py`.

The pinned terminal branch has no root license file. The repository's published
Apache-2.0 license is retained as `UPSTREAM_LICENSE`, read from main at
`cc7ea8f8c16bd2d39a2e4a790cc24b2b370ce1bb`. This distinction is recorded rather
than attributing a nonexistent license file to the terminal commit.

The native method accepts Harbor seed tasks and generates close few-shot
variants, related self-instruct tasks, and depth/breadth evolution variants.
It goes directly from seed selection to artifact authoring; there is no added
task-design model call. The native seed-file filter, per-file 3,500-character
context limit, strategy enumeration and canary removal are retained. Parent
content hashes identify the source before text preprocessing.

Owned adaptations: a typed TerminalDraft replaces the legacy files dictionary;
full text environment context supplements the native filtered prompt so fixtures
can be reconstructed. A fresh offline Harbor baseline/reference pair and bounded
execution-feedback repairs follow static materialization. Dependencies are
installed during image build. The first profile supports a single CPU container
and text fixtures. Models are explicitly configured, not assumed to match the
native DeepSeek default. The original seed domain must survive repairs.

The three released seed examples may be supplied as inputs; any complete Harbor
task is also a native input. Seed data are not packaged in the library. No
upstream project is imported, installed or cloned at runtime. These exports are
generation artifacts, not independent quality acceptance or training approval.
