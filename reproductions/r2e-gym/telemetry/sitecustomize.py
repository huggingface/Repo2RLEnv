"""Measure native OpenAI responses in original CLI processes and workers."""

import os
import sys

if os.environ.get("REPRO_API_RESPONSES") and sys.prefix == "/work/r2e-gym/upstream/.venv":
    from api_usage import record_response
    from openai.resources.chat.completions import Completions

    original = Completions.create

    def measured(self, *args, **kwargs):
        response = original(self, *args, **kwargs)
        record_response(response)
        return response

    Completions.create = measured
