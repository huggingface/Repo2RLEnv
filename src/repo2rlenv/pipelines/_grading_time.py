"""Grading time limits, set from the test runs measured at generation.

Validation runs the reference fix several times through the grading path and
times each run. As in the Kattis problem package format (`ac_to_time_limit`
2.0: the limit is twice the slowest accepted submission), the per-run limit is
twice the slowest measured run, with a floor for cold caches on small suites.
Harbor's verifier timeout then covers the test runs plus the non-test steps,
between Harbor's own default and the cap its SWE adapters use. A task whose
limit would exceed the cap is dropped at generation.

A test run timing out is a graded 0: the submission controls how long the tests
take. Harbor's own timeout firing stays Harbor's ungraded verifier failure.

The limits come from a few runs on the generation host. A slower or
oversubscribed grader (many verifiers per node during RL) can push an honest
suite past them; set `R2E_TIMEOUT_SCALE` in the verifier's environment (Harbor's
job-level `verifier.env`) to scale every per-run limit, and Harbor's
`verifier_timeout_multiplier` to scale the overall verifier timeout.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

RUN_FLOOR_SEC = 120
RUN_FACTOR = 2.0
# Test runs per grading.
RUNS_PER_GRADING = 1
# Copying the image's repository back into /workspace: generous against the
# measured copy, which only the image decides (not the submission).
COPY_FLOOR_SEC = 120
COPY_FACTOR = 5.0
# The verifier's CPU limit (`_pr_runtime_verifier.CPU_LIMIT_SEC`): Harbor's
# timeout must leave room for it, or a log that stalls the parser would end in
# Harbor's ungraded timeout instead of the graded 0 the CPU limit gives.
VERIFIER_PARSE_SEC = 125
VERIFIER_FLOOR_SEC = 600  # Harbor's default verifier.timeout_sec
# Per-run limit for a task emitted without validation (`skip_validation`), when
# nothing was measured: it leaves room in Harbor's default verifier timeout for
# the parse budget and the rebuild, so a hang is still a graded 0 rather than
# Harbor's ungraded timeout.
UNMEASURED_RUN_SEC = 300
VERIFIER_CAP_SEC = 3000  # Harbor's SWE adapters and SWE-bench Pro V2


def _ceil_to(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


@dataclass(slots=True)
class GradingTime:
    """The slowest measured test run and the slowest non-test part of a stage."""

    run_seconds: float
    overhead_seconds: float
    # Slowest measured copy of the image's repository during the rebuild.
    copy_seconds: float = 0.0

    @property
    def run_timeout(self) -> int:
        """Per-run limit passed to the launcher."""
        return max(RUN_FLOOR_SEC, _ceil_to(RUN_FACTOR * self.run_seconds, 30))

    @property
    def copy_timeout(self) -> int:
        """Limit for copying the image's repository (a grader step: ungraded past it)."""
        return max(COPY_FLOOR_SEC, _ceil_to(COPY_FACTOR * self.copy_seconds, 30))

    @property
    def needed_verifier_timeout(self) -> int:
        runs = RUNS_PER_GRADING * self.run_timeout
        return _ceil_to(runs + 1.5 * self.overhead_seconds + VERIFIER_PARSE_SEC + 60, 60)

    @property
    def over_cap(self) -> bool:
        return self.needed_verifier_timeout > VERIFIER_CAP_SEC

    @property
    def verifier_timeout(self) -> int:
        """Harbor's `verifier.timeout_sec` for the task."""
        return min(VERIFIER_CAP_SEC, max(VERIFIER_FLOOR_SEC, self.needed_verifier_timeout))

    def metadata(self) -> dict[str, float | int]:
        return {
            "measured_run_seconds": round(self.run_seconds, 3),
            "measured_overhead_seconds": round(self.overhead_seconds, 3),
            "measured_copy_seconds": round(self.copy_seconds, 3),
            "run_timeout_sec": self.run_timeout,
            "copy_timeout_sec": self.copy_timeout,
            "verifier_timeout_sec": self.verifier_timeout,
        }


def measure(
    samples: list[tuple[float, float]], copies: list[float] | None = None
) -> GradingTime | None:
    """From (test seconds, whole stage seconds) pairs; None without samples.

    With three to five runs a p95 can't be estimated, so the maximum is used.
    `copies` are the measured copies of the image's repository.
    """
    if not samples:
        return None
    return GradingTime(
        run_seconds=max(test for test, _ in samples),
        overhead_seconds=max(max(stage - test, 0.0) for test, stage in samples),
        copy_seconds=max(copies or [0.0]),
    )
