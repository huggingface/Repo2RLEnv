"""Export original optimization tasks through the shared Harbor bundle contract."""

import json
from importlib.resources import files

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle

DOCKERFILE = """FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends bash tmux \\
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 solver && mkdir -p /workspace && chown solver:solver /workspace
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1
"""

RUNTIME_CONTRACT = """## Execution limits

Submit a regular file at `/workspace/solution.py`, no larger than 64 KiB. Each test
starts a fresh isolated Python 3.12 process with one JSON input object on stdin.
Return one JSON output object on stdout. Limits per test are 3 CPU seconds,
5 seconds wall time, 256 MiB address space, and 1 MiB stdout. A missing, malformed,
failing or over-limit submission receives zero on that test. Use only the Python
standard library; no packages, files, subprocesses, network or nondeterminism.
"""


def public_instruction(design):
    if design.instruction.rstrip().endswith(RUNTIME_CONTRACT.rstrip()):
        return design.instruction
    return design.instruction.rstrip() + "\n\n" + RUNTIME_CONTRACT


def export_task(
    design, infrastructure, solution, destination, *, name, org, seed, lineage, resume=False
):
    return write_bundle(
        TaskBundle(
            name=name,
            org=org,
            instruction=public_instruction(design),
            files={
                "environment/Dockerfile": TaskFile.text(DOCKERFILE),
                "solution/solution.py": TaskFile.text(solution.code),
                "solution/solve.sh": TaskFile.text(
                    "#!/bin/bash\nset -eu\ncp /solution/solution.py /workspace/solution.py\n",
                    executable=True,
                ),
                "tests/test.sh": TaskFile.text(
                    "#!/bin/bash\nset -eu\nexec /usr/local/bin/python -I /tests/grade.py\n",
                    executable=True,
                ),
                "tests/grade.py": TaskFile(files(__package__).joinpath("grade.py").read_bytes()),
                "tests/generator.py": TaskFile.text(infrastructure.generator),
                "tests/scorer.py": TaskFile.text(infrastructure.scorer),
                **(
                    {"tests/feasibility.py": TaskFile.text(infrastructure.feasibility)}
                    if infrastructure.feasibility
                    else {}
                ),
                "tests/contract.json": TaskFile.text(
                    json.dumps(
                        {"seed": seed, "explicit_feasibility": bool(infrastructure.feasibility)}
                    )
                ),
            },
            metadata={
                "pipeline": "optimization_synth",
                "recipe": "frontiersmith",
                "recipe_version": "1",
                "paper": "https://arxiv.org/abs/2605.14445",
                "implementation": "paper_inspired",
                "reward_kinds": ["optimization_score"],
                "quality_status": "exported",
                "oracle_semantics": "best_sampled_feasible_solution_not_proven_optimum",
                "language": "python",
                "mutation": design.mutation,
                **lineage,
            },
            agent={"user": "solver", "network_mode": "no-network"},
            verifier={"user": "root", "network_mode": "no-network"},
            verifier_timeout_sec=150,
        ),
        destination,
        resume=resume,
    )
