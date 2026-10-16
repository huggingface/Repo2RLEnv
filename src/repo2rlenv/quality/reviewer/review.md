This run uses evidence-review-v2. Return EvidenceReview rather than the legacy
Review. The controller derives task/verifier/leakage aggregates and the disposition.

First trace the active verifier entrypoint through its commands, early exits and
preconditions to the final reward writer. Establish which tests actually execute
and how their outcomes determine the reward. Merely having good tests, a grader
or a passing reference somewhere in the directory does not establish that this
entrypoint uses them. Cite the entry script in the gaming assessment, and explain
its effect. Inspect shell guards and overwritten rewards as well as Python tests.
Compare any extra submission requirements with the full public instruction.

Assess all seven criteria exactly once:
- alignment: every graded requirement is part of the public contract;
- feasibility: the request is coherent and required resources/assets are available;
- reference: the reference actually implements the public behavior;
- coverage: central requested behavior is tested, including meaningful wrong solutions;
- alternatives: grading permits other implementations that satisfy the public contract;
- gaming: outputs and reward cannot be accepted through a concrete shortcut;
- leakage: the actual learner view does not reveal private solution or grading answers.

Use pass/fail/unknown, supported by exact citations. A static pass is a finding
about inspected evidence, not proof that execution succeeded. If essential tests
or assets cannot be inspected, use unknown. Optional polish is advisory.

Every failed criterion needs a blocking finding, and blocking findings must attach
to a failed criterion. Name the precise public requirement and conflicting check
or missing behavior, the grading consequence, and the smallest component to repair.
Attribution is task, solver, infrastructure, mixed, or unresolved. A solver failure
or provider error alone cannot justify a blocking task finding. A diagnosed broken
probe may need instrumentation repair; identify it as category probe.

Distinguish static evidence (source inspected), observed evidence (completed bound
trial logs/results), and derived evidence (explain the calculation from cited inputs).
No execution is implied by source inspection, a planned script or a claimed trace.
Confidence is an ordinal self-assessment, not a probability or substitute for evidence.

Keep a short investigation record for material hypotheses, including dismissed
alternatives. Do not invent probes you did not run. Investigations that conclude
anything require exact supporting citations. Unsupported hypotheses stay unresolved
and do not become blocking findings.

Check the narrowest actual contract before rejecting unusual implementation details.
An explicitly supplied premise is not automatically an answer leak. A private
function, cache attribute or exact error string is not a public requirement merely
because the reference uses it. Conversely, preserve explicitly requested interfaces
and numerical behavior. Do not broaden a task simply to accommodate a solver patch.

For rollout interpretation, follow the original instructions and receipt checks.
Correct work rejected and incorrect work accepted both require investigation.
Use existing bounded read requests and permitted semantic probes; do not request
more evidence once a supported material decision is available.
