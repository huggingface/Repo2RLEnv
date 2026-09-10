"""Use Endless's original converter, then fill its missing Harbor directory layout."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from generator.convert_to_harbor.add_reward_file import update_test_sh
from generator.convert_to_harbor.convert_sif_docker import process_task_directory
from harbor.models.task.task import Task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("native", type=Path)
    parser.add_argument("converted", type=Path)
    parser.add_argument("harbor", type=Path)
    args = parser.parse_args()
    records = []
    for source in sorted(args.native.glob("task_*")):
        converted = args.converted / source.name
        shutil.copytree(source, converted)
        # Use the released per-task API. The batch CLI's hardcoded o3 pass@16
        # dataset selection is separate from converting a newly generated task.
        result = process_task_directory(converted, model="gpt-4o", provider="openai")
        records.append(result)
        args.converted.mkdir(parents=True, exist_ok=True)
        (args.converted / "conversion-results.json").write_text(json.dumps(records, indent=2))
        if not result["success"]:
            continue
        destination = args.harbor / source.name
        (destination / "environment").mkdir(parents=True)
        (destination / "tests").mkdir()
        meta = json.loads((source / "task.json").read_text())
        (destination / "instruction.md").write_text(meta["description"])
        shutil.copy2(converted / "Dockerfile", destination / "environment/Dockerfile")
        shutil.copy2(source / "test_final_state.py", destination / "tests/test_final_state.py")
        (destination / "tests/test.sh").touch()
        update_test_sh(destination / "tests/test.sh")
        (destination / "task.toml").write_text("""version = "1.0"
[metadata]
source = "Endless Terminals upstream reproduction"
[agent]
timeout_sec = 600
[verifier]
timeout_sec = 300
[environment]
build_timeout_sec = 600
cpus = 1
memory_mb = 2048
allow_internet = true
""")
        Task(destination)
        result["harbor"] = str(destination)
        result["oracle_present"] = False
        (args.converted / "conversion-results.json").write_text(json.dumps(records, indent=2))
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
