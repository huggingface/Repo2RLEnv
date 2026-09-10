"""Restore the command field expected by the released value scorer.

The pinned signals_to_dict omits commands_with_output and load_recording now
returns strings, while score_value expects command dictionaries. Preserve the
original score prompts, thresholds and decisions; record this compatibility fix.
"""

import json
import runpy
import sys
from pathlib import Path

import analyze_recording

original = analyze_recording.signals_to_dict


def complete_signals(signals):
    result = original(signals)
    result["commands_with_output"] = [
        {"command": command, "output": ""} if isinstance(command, str) else command
        for command in signals.commands_with_output
    ]
    return result


analyze_recording.signals_to_dict = complete_signals
Path("/evidence/terminalworld/scorer-compat.json").write_text(
    json.dumps(
        {
            "change": "Restore omitted command list and normalize string commands to the dict contract",
            "command_source": "Original load_recording uses the extracted solve.sh",
            "output_evidence": "No stdout is fabricated; original loader does not retain it",
            "unchanged": ["LLM prompts", "feasibility rules", "score thresholds", "models"],
        },
        indent=2,
    )
)
sys.argv.insert(1, "data_filtering.score_value")
runpy.run_path("/work/recipes/terminalworld/native_driver.py", run_name="__main__")
