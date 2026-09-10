"""Dispatch original TerminalWorld CLI modules with API response receipts."""

import runpy
import sys

import litellm
from api_usage import record_response

original_completion = litellm.completion


def measured_completion(*args, **kwargs):
    kwargs["max_tokens"] = min(kwargs.get("max_tokens") or 4096, 4096)
    kwargs["timeout"] = min(kwargs.get("timeout") or 180, 180)
    response = original_completion(*args, **kwargs)
    record_response(response)
    return response


litellm.completion = measured_completion
module = sys.argv.pop(1)
if module not in {
    "data_filtering.score_value",
    "task_synthesis.extract_solution",
    "task_synthesis.generate_instruction",
    "test_generation.generate_tests",
}:
    raise ValueError(module)
runpy.run_module(module, run_name="__main__")
