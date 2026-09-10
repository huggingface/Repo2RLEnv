"""Run the original R2E CLI with API usage receipts; preserve native prompts."""

import runpy

from api_usage import record_response
from openai.resources.chat.completions import Completions

original = Completions.create


def measured(self, *args, **kwargs):
    response = original(self, *args, **kwargs)
    record_response(response)
    return response


Completions.create = measured
if __name__ == "__main__":
    runpy.run_module("r2e.cli", run_name="__main__")
