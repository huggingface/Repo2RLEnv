# Recorded deviations

`001-resource-limits.patch` adds $8/100-turn SDK session limits and flushes the
existing author logs. It preserves the original **claude-sonnet-4-6** model, prompts,
tools, hooks and acceptance criteria. The input is the first newly generated SETA
Harbor task, with parent provenance retained. The smoke uses one original
CHANGE_CONTEXT evolution round and one variant. Model training and the separate
upstream learner rollout pipeline are outside this generation smoke.
