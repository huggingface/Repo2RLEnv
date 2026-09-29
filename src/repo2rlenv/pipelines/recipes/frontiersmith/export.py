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


def export_task(
    design, infrastructure, solution, destination, *, name, org, seed, lineage, resume=False
):
    return write_bundle(
        TaskBundle(
            name=name,
            org=org,
            instruction=design.instruction,
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
                "tests/contract.json": TaskFile.text(json.dumps({"seed": seed})),
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
