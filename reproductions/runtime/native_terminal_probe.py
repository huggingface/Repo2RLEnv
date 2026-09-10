"""Probe native terminal tasks and replay an original successful solver as a witness.

Run only in the remote worker, with the selected upstream repository importable.
No prompts or generated tests are rewritten here.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", choices=["endless", "tmax"])
    parser.add_argument("task", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--solutions", type=int, default=1)
    parser.add_argument("--base-sifs", type=Path)
    parser.add_argument("--harbor-task", type=Path)
    parser.add_argument("--max-actions", type=int, default=20)
    args = parser.parse_args()
    prefix = "generator" if args.project == "endless" else "rl_data.generator"
    module = importlib.import_module(prefix + ".sample_solutions")
    environment = importlib.import_module(prefix + ".env").InteractiveContainerEnvironment
    args.output.mkdir(parents=True, exist_ok=False)
    if not (args.task / "container.sif").exists() and not args.base_sifs:
        # The released generation CLI validates in a temporary image, then omits
        # the persistent SIF. Materialize the unchanged definition for its solver.
        command = [
            "apptainer",
            "build",
            str(args.task / "container.sif"),
            str(args.task / "container.def"),
        ]
        (args.output / "build-command.json").write_text(json.dumps(command))
        with (args.output / "build.log").open("w") as stream:
            subprocess.run(
                command, stdout=stream, stderr=subprocess.STDOUT, timeout=600, check=True
            )
    parameters = {
        "container_sif_path": str(args.task / "container.sif"),
        "initial_test_path": str(args.task / "test_initial_state.py"),
        "final_test_path": str(args.task / "test_final_state.py"),
        "def_path": str(args.task / "container.def"),
    }
    if args.base_sifs:
        parameters["base_sifs_dir"] = str(args.base_sifs)
    environment_parameters = dict(parameters)
    if args.project == "tmax":
        # Match its released solver's command_timeout default, not the lower
        # standalone environment constructor default.
        environment_parameters["read_timeout"] = (
            inspect.signature(module.run_n_solutions).parameters["command_timeout"].default
        )
    env = environment(**environment_parameters, verbose=True)
    try:
        initialized = env.initialize(run_initial_tests=True)
        final = env.run_final_tests() if initialized else (False, "initialization failed")
        completed = initialized and "Command timed out" not in final[1]
        record = {
            "initialized": initialized,
            "nop_verifier_completed": completed,
            "nop_final_success": final[0] if completed else None,
            "output": final[1],
        }
        (args.output / "native-nop.json").write_text(json.dumps(record, indent=2))
    finally:
        env.cleanup()
    if not initialized:
        raise RuntimeError("Native environment failed to initialize")
    if not completed:
        raise RuntimeError(
            "Native no-op verifier did not complete; inspect before spending on a solver"
        )
    if record["nop_final_success"]:
        print(
            json.dumps({"native_nop": record, "solver_skipped": "already solved in initial state"})
        )
        return
    if not args.solutions:
        return
    call_parameters = {
        **parameters,
        "num_solutions": args.solutions,
        "task_path": str(args.task / "task.json"),
        "model": args.model,
        "max_actions": args.max_actions,
        "max_tokens": 4096,
        "num_pool_workers": 1,
        "save_dir": str(args.output / "solutions"),
        "verbose": True,
    }
    supported = inspect.signature(module.run_n_solutions).parameters
    unsupported = set(call_parameters) - set(supported)
    if unsupported:
        raise ValueError(f"Upstream solver API differs: {unsupported}")
    result = module.run_n_solutions(**call_parameters)
    (args.output / "native-solutions.json").write_text(json.dumps(result, indent=2))
    successes = [row for row in result.get("results", []) if row.get("success")]
    if args.harbor_task and successes:
        commands = []
        for message in successes[0]["messages"]:
            if message["role"] != "assistant":
                continue
            if args.project == "endless":
                action = module._extract_action(message["content"])
                if action["type"] == "command":
                    commands.append(action["command"])
            else:
                action = module._extract_tool_call(message)
                if action["type"] in {"command", "done"} and action["command"]:
                    # Preserve the actual persistent PTY implementation, including submission.
                    # Its system prompt says subshells, but env.exec uses brace groups.
                    commands.append(action["command"])
        solution = args.harbor_task / "solution"
        solution.mkdir(exist_ok=False)
        (solution / "solve.sh").write_text(
            "#!/bin/bash\n# Derived replay of an original successful upstream solver trace.\n"
            "# Preserve the interactive agent's ability to continue after a failed command.\n"
            "cd /home/user\n" + "\n".join(commands) + "\ntrue\n"
        )
        (solution / "witness-provenance.json").write_text(
            json.dumps(
                {
                    "source": str(args.output / "native-solutions.json"),
                    "successful_result_index": result["results"].index(successes[0]),
                    "model": args.model,
                    "command_count": len(commands),
                    "reference_type": "derived_upstream_solver_replay",
                },
                indent=2,
            )
        )
    print(json.dumps({"native_nop": record, "solver_successes": len(successes)}, indent=2))


if __name__ == "__main__":
    main()
