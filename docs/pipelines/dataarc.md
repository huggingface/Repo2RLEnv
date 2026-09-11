# `terminal_synth / dataarc`

DataArc augments complete Harbor tasks using its released terminal synthesis
workflow. It preserves four distinct transformations:

```text
Harbor seed tasks → filtered instruction/reference/test context
                 → few-shot: close variant
                 → self-instruct: related task in the same domain
                 → evolution: deeper constraint or neighboring task type
                 → complete instruction, environment, reference and tests
                 ↺ bounded repairs from real execution feedback
                 → fresh offline baseline/reference → Harbor export
```

Run `repo2rlenv generate --config examples/owned-dataarc.yaml`. The input directory
contains seed task subdirectories, each with `task.toml`, `instruction.md` and
`solution/solve.sh`. The first profile accepts text assets in a single CPU Linux
container. Native examples cover portfolio optimization, asynchronous task
cancellation and constraint scheduling. Users may supply their own Harbor seeds.

`strategies`, `evol_directions` and `samples_per_strategy` control enumeration.
Each variant is authored directly from its seed. There is no separate design
model. The seed's domain and tools must survive any execution-informed repairs.
Parent hashes and strategy names remain in the generated lineage.

Shared options control `target`, `max_candidates`, `max_repairs`, token limits
and test timeouts. The initial target is **20 generated tasks**. Baseline failure
and reference success are generation checks; detailed quality validation follows
the full generation campaign. See the [remote execution and CLI guide](owned_recipes.md).

Credit: [DataArc-SynData-Toolkit](https://github.com/DataArcTech/DataArc-SynData-Toolkit),
terminal branch `2a1d65ec8dcfaea2458d67e1fb18078cce6420b9`. The repository publishes
an Apache-2.0 license on main; the pinned terminal branch omits that file. The
packaged provenance records both revisions. See [RFC 0022](../rfcs/0022-dataarc-terminal-recipe.md)
for the contract and `recipes/dataarc/provenance.md` for exact adaptations.
