"""Native annotation CLI with private credential binding and a response cap."""

import os
import runpy
import sys

from api_usage import record_response
from openai.resources.chat.completions import Completions

original = Completions.create


def measured(self, *args, **kwargs):
    kwargs.setdefault("max_tokens", 4096)
    response = original(self, *args, **kwargs)
    record_response(response)
    return response


Completions.create = measured
if __name__ == "__main__":
    # The upstream CLI requires this argument. Bind it only in interpreter
    # memory, never a shell command, saved configuration or process argv.
    sys.argv.extend(["--api-key", os.environ["OPENAI_API_KEY"]])
    runpy.run_path(
        "/work/swe-rebench-v2/upstream/scripts/annotation_script.py", run_name="__main__"
    )
