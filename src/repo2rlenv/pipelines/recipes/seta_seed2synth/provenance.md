# SETA Seed2Synth attribution

Inspired by and adapted from camel-ai/SETA, Apache-2.0, commit
`e4715b01174e6c9503fc46120d81dd692ced75e6`.

- `idea_prompt.md`: upstream `datasynth/seed2synth_pipeline/agents/seed2idea_prompts/idea_agent_base_prompt.md`.
- `builder_prompt.md`: upstream `datasynth/seed2synth_pipeline/agents/datapoint_agent_guide/agent.md`.
- Algorithm: `datasynth/seed2synth_pipeline/seed2task_pipeline.py`.

The two prompt files are retained from that revision; runtime additions are in
owned Python modules. The upstream license is included as `UPSTREAM_LICENSE`.

Owned adaptations: JSON seed shards replace source-folder I/O; structured model
responses replace the Camel/Claude agent file-writing interface; the controller
materializes artifacts and returns actual Harbor execution failures for bounded
repair. The CPU Debian/Python profile preinstalls dependencies and disables runtime
network access. Owned schema 1.3 emission and JUnit reward parsing replace legacy
task.toml and network-installing test-runner templates. Per-test weights and author
self-review are retained as evidence; they do not imply independent acceptance.

This is an owned workflow adaptation, not byte-identical upstream execution.
No upstream project is installed, imported or cloned by the recipe at runtime.

The owned design response has a 9,000-token ceiling and an explicit 18,000-character draft limit. Design sections describe the build without embedding complete implementation files; the artifact author writes those separately. This bounds truncation seen in the generation campaign.
