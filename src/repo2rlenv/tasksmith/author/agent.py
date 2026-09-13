"""Only explicit controller tools are available to the author runtime."""

from repo2rlenv.tasksmith.author.external_agent import run_external_agent

SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Run a bounded command in the remote builder sandbox. Never runs on the controller. Inspect repository files and existing tests; do not modify the pinned checkout.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


async def run_agent(*, runtime, **kwargs):
    return await run_external_agent(engine=runtime, **kwargs)
