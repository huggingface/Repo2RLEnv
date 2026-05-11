"""Parser + action-handling logic for the bootstrap agent loop.

No live Docker, no live LLM — these are pure-logic tests.
"""

from __future__ import annotations

import pytest

from repo2rlenv.bootstrap.agent import _parse_save_setup, parse_action


def test_parse_bash_action():
    text = "Thought: explore the repo\nAction: BASH\nInput: ls /workspace\n\n"
    thought, action = parse_action(text)
    assert thought == "explore the repo"
    assert action.name == "BASH"
    assert action.input == "ls /workspace"


def test_parse_save_setup_action():
    text = (
        "Thought: done\n"
        "Action: SAVE_SETUP\n"
        'Input: {"rebuild_cmds": ["pip install -e ."], "test_cmds": ["pytest"]}'
    )
    thought, action = parse_action(text)
    assert action.name == "SAVE_SETUP"
    payload = _parse_save_setup(action.input)
    assert payload["rebuild_cmds"] == ["pip install -e ."]
    assert payload["test_cmds"] == ["pytest"]


def test_parse_save_setup_strips_code_fences():
    payload = _parse_save_setup('```json\n{"rebuild_cmds": [], "test_cmds": []}\n```')
    assert payload == {"rebuild_cmds": [], "test_cmds": []}


def test_parse_save_setup_rejects_missing_keys():
    with pytest.raises(ValueError, match="rebuild_cmds"):
        _parse_save_setup('{"test_cmds": []}')


def test_parse_save_setup_rejects_non_list():
    with pytest.raises(ValueError, match="list"):
        _parse_save_setup('{"rebuild_cmds": "make", "test_cmds": []}')


def test_parse_action_invalid_format():
    _, action = parse_action("just some text the model emitted, no action header")
    assert action.name == "INVALID"


def test_parse_action_unknown_tool():
    text = "Thought: ?\nAction: TELEPORT\nInput: somewhere\n\n"
    _, action = parse_action(text)
    assert action.name == "INVALID"


def test_parse_action_case_insensitive_tool_name():
    text = "Thought: ?\nAction: bash\nInput: ls\n\n"
    _, action = parse_action(text)
    assert action.name == "BASH"


def test_parse_action_multiline_input():
    text = (
        "Thought: install everything\n"
        "Action: BASH\n"
        "Input: apt-get update && \\\n"
        "       apt-get install -y build-essential\n\n"
    )
    _, action = parse_action(text)
    assert action.name == "BASH"
    assert "apt-get install" in action.input
