# `reasoning_synth / scaler`

SCALER expands released parameterized problem families into concrete reasoning
tasks. It uses the supplied generator and reference programs without an LLM call.

```text
Family JSON: description + generator + references + difficulty mapping
  → select difficulty and random seed → scale parameters
  → remote generator → program input + concrete problem description
  → remote reference program → expected answer
  → Harbor answer-file task with private expected answer
  → fresh no-op/reference rewards −1/+1 → export
```

Run `repo2rlenv generate --config examples/owned-scaler.yaml`. The input is a native
mapping such as the released `SCALER-data/train/SCALER-8.json`, or the same format
with user-supplied families. All supplied code executes inside offline containers
on Modal or Daytona. The controller only reads JSON and packages artifacts.

`difficulties`, `samples_per_difficulty`, `seed`, `target`, `max_candidates` and
execution bounds control the batch. Input hashes, actual random seeds, scaled
parameters and reference-code hashes are recorded. Duplicate concrete instances
are rejected. The first target is **20 distinct reasoning tasks**; this is not a
claim of twenty new families or twenty repository coding tasks.

The learner writes its boxed answer to `/workspace/answer.txt`. A separate
verifier applies the active upstream math-verify metric and preserves its native
−1/+1 reward. The reference answer never enters the learner image. Generation
checks confirm execution and packaging; detailed quality acceptance comes later.

This profile expands existing families. Fresh-family synthesis, adaptive
difficulty training and the verl training stack are outside its scope. See the
[shared CLI and cloud guide](owned_recipes.md).

Credit: [SCALER](https://github.com/ALEX-nlp/SCALER), Apache-2.0, commit
`60c6c5037866c718f4c001ea338f9c5a91cb01ae`.
[RFC 0026](../rfcs/0026-scaler-recipe.md) and packaged
`recipes/scaler/provenance.md` map the source and runtime adaptations.
