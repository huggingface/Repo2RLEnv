"""Bind the user's credential in memory and run the original SecVerifier CLI."""

import os
import runpy

import litellm
import openhands.core.config
from api_usage import record_response
from pydantic import SecretStr

original_config = openhands.core.config.get_llm_config_arg
original_completion = litellm.completion


def credential_config(*args, **kwargs):
    config = original_config(*args, **kwargs)
    if config is not None:
        config.api_key = SecretStr(os.environ["OPENAI_API_KEY"])
    return config


def measured_completion(*args, **kwargs):
    response = original_completion(*args, **kwargs)
    record_response(response)
    return response


openhands.core.config.get_llm_config_arg = credential_config
litellm.completion = measured_completion
if __name__ == "__main__":
    runpy.run_path("/work/secverifier/upstream/multi-agent.py", run_name="__main__")
