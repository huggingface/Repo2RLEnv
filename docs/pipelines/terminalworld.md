# `terminal_reconstruct / terminalworld`

TerminalWorld reconstructs executable tasks from real terminal recordings.
Its tests are generated from the public goal and observed reference execution.

```text
Public metadata + text transcript → native transcript screen + value scoring
                                 → extract and refine the recorded solution
                                 → outcome-oriented instruction
                                 → reconstruct dependencies and initial files
                                 ↺ remote build/reference replay and repair
                                 → initial/final filesystem changes
                                 → snapshot-informed state tests
                                 → fresh Harbor baseline/reference → export
```

Provide a directory containing one or more native recording folders, each with
`info.json` and `recording.txt`. Metadata includes `title`, `description`, `id`
and `url`. To acquire public source recordings from numeric IDs:

```bash
python -m repo2rlenv.pipelines.recipes.terminalworld.source \
  --ids-json workspace/recording-ids.json --out workspace/recordings
repo2rlenv generate --config examples/owned-terminalworld.yaml
```

The ID file is a JSON list such as `["100135"]`. Acquisition fetches text and
metadata only, checks robots.txt and records download failures. Existing inputs
are reused. Export the generated task bundles rather than the raw recordings.

The first runtime profile supports a single offline CPU Linux container. It
installs real dependencies during build and can synthesize missing input files
when the recorded workflow provides enough evidence, as permitted by the native
builder. It excludes opaque TUI, GPU, privileged networking, multi-service and
external-account workflows. The value score is an upstream input-selection
stage, separate from the later task-quality audit.

Options include the shared terminal generation bounds and `min_score` (default
four out of twelve, the native bronze threshold). The snapshot records file
changes and bounded content prefixes. The test author consumes that execution
evidence before the fresh baseline/reference trials.

The target is **20 generated tasks**. Partial-solution probes, independent
leakage/shortcut checks and blind solver rollouts follow the generation campaign.
An export does not claim the upstream three-trial acceptance result. Cloud
workers, metering and Rich/JSON progress use the [shared interface](owned_recipes.md).

Credit: [TerminalWorld](https://github.com/EuniAI/TerminalWorld), Apache-2.0,
commit `784698ba93735470ce1664bff2ec44bcd7b28e15`. See
[RFC 0019](../rfcs/0019-terminalworld-recipe.md) and packaged
`recipes/terminalworld/provenance.md` for the exact source map and adaptations.
