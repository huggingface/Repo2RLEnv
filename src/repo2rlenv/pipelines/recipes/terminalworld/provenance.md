# TerminalWorld adaptation

Source: [EuniAI/TerminalWorld](https://github.com/EuniAI/TerminalWorld), commit
`784698ba93735470ce1664bff2ec44bcd7b28e15`, Apache-2.0. `UPSTREAM_LICENSE`
retains the original notice. Retained prompts come from
`data_filtering/score_value.py`, `task_synthesis/extract_solution.py`,
`task_synthesis/generate_instruction.py`, `environment_building/skill/workflow/STEP1.md`
and `test_generation/generate_tests.py`. `privacy.py` retains the regular
expressions and category map from `data_filtering/detect_pii.py`.

The owned workflow follows recording acquisition/screening, trajectory scoring,
solution extraction/refinement, goal-oriented instruction, environment building,
reference replay, snapshot-based test synthesis, and fresh baseline/reference
execution. It retains separate solution and instruction authors. Tests are
authored after executing the reference, using observed filesystem changes.

The first profile accepts the native `info.json` / `recording.txt` directory
layout. The optional acquisition command fetches public metadata and `.txt`
only, checks robots.txt, bounds response sizes and records failures. It does not
fetch raw cast files or media. Raw transcripts are generation inputs, not files
to redistribute with the resulting Harbor task. The exported lineage retains
the source URL and transcript hash. Flagged transcript text is removed before
model input while its screening categories and hash are retained.

The native three value dimensions and thresholds are retained, with a default
minimum combined score of four (bronze). Runtime feasibility and command count
are elicited in that call rather than importing the separate upstream signal
analyzer. The long prompt is chosen by transcript length rather than a parsed
command threshold. Context-link probes are bounded to three URLs and an explicit
set of public code/documentation hosts; this can undercount the native context
score for other sites. Those changes are generation-profile limitations.

Structured environment and reference output replaces the upstream coding-agent
file-editing loop. The remote worker builds real dependencies, replays the
reference in a fresh offline container, and records created, modified and
deleted files plus bounded content prefixes. Content hashes complement file
metadata; the snapshot is bounded evidence, not a complete filesystem archive.
Missing fixtures may be synthesized only as permitted by the native builder
workflow and must be described in the builder's self-review.

This profile uses one CPU Linux container. GPU, TUI, privileged networking,
multi-service Compose and external-account workflows are not supported.
Five to ten named completion tests use the common weighted Harbor reward.
Native partial-solution trials and detailed independent quality audits are
deferred to the campaign's evaluation phase. Generation success does not claim
native acceptance or training approval. No research repository is installed,
imported or cloned by the owned implementation.
