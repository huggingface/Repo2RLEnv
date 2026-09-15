# R2E adaptation

Source: [r2e-project/r2e](https://github.com/r2e-project/r2e), commit
`bcbed156711bb939de14aa46b27eee15073f5272`, MIT. `UPSTREAM_LICENSE` retains the
original notice. `test_prompt.md` retains `SYSTEM_MESSAGE` from
`generators/testgen/prompt.py`; `specification_prompt.md` retains
`SYSTEM_MESSAGE_TESTS` from `generators/specgen/prompt.py`.

The owned pipeline follows `generators/testgen/genexec.py`: generate differential
unittests, execute them, feed back errors or insufficient branch coverage, and
repeat for a bounded number of rounds. The native default branch threshold is
0.8. Specification refinement consumes executed tests and observed input/output
types, following `generators/specgen/`.

`extract.py` implements a bounded module-level dependency slice, inspired by
`pat/dependency_slicer/`, without importing the upstream extractor. The initial
profile supports documented synchronous top-level functions in ordinary Python
package layouts with a `tests/` directory. It uses the complete working repository
for execution rather than rebuilding a standalone sliced import environment.

The reference API remains `reference_<function>` in private `fut_module`. The
reference implementation and generated tests exist only in the separate verifier
image; the learner image contains neither. The first profile loads Python source
inside that private test process instead of using upstream's compiled reference
machinery. Its reference-access behavior during adversarial grading still needs
the later quality audit. Generated is not quality-accepted.

Coverage.py measures branches in the actual target module only while generated
tests execute. Existing repository tests also run for regression contrast, but
their calls do not inflate the generated-test coverage score. Three rounds are
the default. The upstream debug-only fifteen-function slice is replaced by the
explicit `max_candidates` option so the campaign can reach twenty tasks.

Essential libraries and the existing cloud/bootstrap/Harbor code are reused. No
research repository is installed, imported or cloned by this recipe at runtime.
