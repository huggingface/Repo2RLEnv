"""Recover a single original TMax rollout from response receipts, without new calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rl_data.generator.env import InteractiveContainerEnvironment
from rl_data.generator.sample_solutions import _extract_tool_call

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("task", type=Path)
parser.add_argument("receipts", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("harbor", type=Path)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
rows = [json.loads(x) for x in args.receipts.read_text().splitlines()]
commands = []
for row in rows:
    action = _extract_tool_call(row["response"]["choices"][0]["message"])
    if action["type"] in {"command", "done"} and action["command"]:
        commands.append(action["command"])
assert commands, "No recorded native commands"
env = InteractiveContainerEnvironment(
    container_sif_path=str(args.task / "container.sif"),
    initial_test_path=str(args.task / "test_initial_state.py"),
    final_test_path=str(args.task / "test_final_state.py"),
    def_path=str(args.task / "container.def"),
    read_timeout=120,
    verbose=True,
)
report = {
    "source": str(args.receipts),
    "reference_type": "recovered single-sample native API trace replay",
    "commands": [],
}
try:
    report["initialized"] = env.initialize(run_initial_tests=True)
    assert report["initialized"]
    for command in commands:
        success, output = env.exec(command)
        report["commands"].append({"command": command, "success": success, "output": output})
        (args.output / "replay.json").write_text(json.dumps(report, indent=2))
    success, output = env.run_final_tests()
    report["final_success"] = success
    report["final_output"] = output
    (args.output / "replay.json").write_text(json.dumps(report, indent=2))
    if success:
        solution = args.harbor / "solution"
        solution.mkdir(exist_ok=False)
        (solution / "solve.sh").write_text(
            "#!/bin/bash\n# Replay of recorded original TMax solver calls.\ncd /home/user\n"
            + "\n".join(commands)
            + "\ntrue\n"
        )
        (solution / "witness-provenance.json").write_text(
            json.dumps(
                {
                    "source": str(args.receipts),
                    "validation": str(args.output / "replay.json"),
                    "reference_type": report["reference_type"],
                    "command_count": len(commands),
                },
                indent=2,
            )
        )
finally:
    env.cleanup()
print(json.dumps({"final_success": report.get("final_success"), "commands": len(commands)}))
