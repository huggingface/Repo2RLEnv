"""Invoke original CLI-Gym with measured responses and bounded API requests."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import litellm
from api_usage import record_response

original_completion = litellm.completion


def measured_completion(*args, **kwargs):
    # Resource-control deviation only: preserve native prompts and parsing.
    kwargs["max_tokens"] = min(kwargs.get("max_tokens") or 4096, 4096)
    kwargs["timeout"] = min(kwargs.get("timeout") or 180, 180)
    kwargs["max_retries"] = 0
    response = original_completion(*args, **kwargs)
    record_response(response)
    return response


litellm.completion = measured_completion
random.seed(42)

if sys.argv[1] == "generate-config":
    from cli_gym.build_destruction_task import gen_destruction_task

    result = gen_destruction_task(
        repo_name="addict",
        agent="terminus-2",
        candidate_uts_file="./CLI-Gym/UTs/UT_addict.json",
        directions="Tamper with configuration files",
        output_base_dir="./CLI-Gym/destruction_tasks-config",
    )
    assert Path(result, "full_task.json").is_file()
elif sys.argv[1] in {"assemble", "assemble-config"}:
    from cli_gym.assemble_problem_instance import assemble_problem_instance

    suffix = "-config" if sys.argv[1] == "assemble-config" else ""
    runs = sorted(Path("/work/cli-gym/native-runs" + suffix).glob("*/results.json"))
    assert len(runs) == 1, f"Expected one native run, found {len(runs)}"
    instances = assemble_problem_instance(
        repo_name="addict",
        result_dir=str(runs[0].parent),
        original_task_dir=f"./CLI-Gym/destruction_tasks{suffix}/addict",
    )
    Path("/evidence/cli-gym/assembly.json").write_text(
        json.dumps(
            {
                "instances": instances,
                "count": len(instances),
                "note": "Two instruction variants per environment; no native restoration oracle.",
            },
            indent=2,
        )
    )
    assert instances, "Original assembler emitted no instances; inspect native evidence"
else:
    from cli_gym.cli import main

    main()
