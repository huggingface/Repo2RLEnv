"""Response-only telemetry inherited by the original pipeline's Python children."""

import json
import os
import sys
from pathlib import Path

if os.environ.get("REPRO_API_RESPONSES") and sys.prefix == "/work/swe-next/venv":
    import requests
    from api_usage import record_response
    from openai.resources.chat.completions import Completions

    original_create = Completions.create
    original_post = requests.post

    def measured_create(self, *args, **kwargs):
        response = original_create(self, *args, **kwargs)
        record_response(response)
        return response

    def measured_post(url, *args, **kwargs):
        response = original_post(url, *args, **kwargs)
        if url.startswith("https://api.anthropic.com/"):
            with Path("/evidence/swe-next/anthropic-api.jsonl").open("a") as stream:
                stream.write(
                    json.dumps({"status_code": response.status_code, "response": response.json()})
                    + "\n"
                )
        return response

    Completions.create = measured_create
    requests.post = measured_post
