"""Run the original DataArc example; capture the provider response for costing."""

import runpy

from api_usage import record_response
from openai.resources.chat.completions import Completions

original_create = Completions.create


def measured_create(self, *args, **kwargs):
    response = original_create(self, *args, **kwargs)
    record_response(response)
    return response


Completions.create = measured_create
runpy.run_path(
    "/work/dataarc-terminal/upstream/examples/syn_agentic_data/run_terminal_bench.py",
    run_name="__main__",
)
