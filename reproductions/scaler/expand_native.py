"""Expand one released SCALER family through its original generator and references.

This executes existing family code; it does not reproduce new family synthesis.
"""

import hashlib
import json
from pathlib import Path

import requests
from generate_problem_from_environment import get_problems
from logger import setup_logger

ROOT = Path("/work/scaler")
EVIDENCE = Path("/evidence/scaler")
source = ROOT / "upstream/SCALER-data/train/SCALER-8.json"
families = json.loads(source.read_text())
family_id = next(iter(families))
family = families[family_id]
original_post = requests.post


def recorded_post(url, *args, **kwargs):
    if url != "http://127.0.0.1:8080/run_code":
        raise ValueError("Native execution must target the private SandboxFusion service")
    response = original_post(url, *args, **kwargs)
    with (EVIDENCE / "expansion-execution.jsonl").open("a") as stream:
        stream.write(
            json.dumps({"request": kwargs.get("json"), "response": response.json()}) + "\n"
        )
    return response


requests.post = recorded_post
records = []
for difficulty in [4, 8, 12]:
    pairs = get_problems(
        family, [difficulty], "http://127.0.0.1:8080/run_code", logger=setup_logger()
    )
    assert len(pairs) == 1 and "None" not in pairs[0][1]
    instruction, reference_answer = pairs[0]
    records.append(
        {
            "family": family_id,
            "difficulty": difficulty,
            "instruction": instruction,
            "reference_answer": reference_answer,
            "generation": "released family expansion",
        }
    )
output = ROOT / "native-output"
output.mkdir(exist_ok=True)
(output / "instances.json").write_text(json.dumps(records, indent=2))
(output / "family.json").write_text(json.dumps(family, indent=2))
(EVIDENCE / "expansion-summary.json").write_text(
    json.dumps(
        {
            "source_dataset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "family": family_id,
            "fresh_instances": len(records),
            "new_families": 0,
            "native_entrypoint": "generate_problem_from_environment.get_problems",
            "native_domain": "concrete algorithmic reasoning",
            "llm_calls": 0,
        },
        indent=2,
    )
)
print(json.dumps({"family": family_id, "instances": len(records)}))
