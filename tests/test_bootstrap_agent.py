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
    _, action = parse_action(text)
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


def test_parse_action_strips_hallucinated_observation_tail():
    """Some models write the command AND simulate its observation inline.

    The fastapi/fastapi failure on Sonnet had inputs like:
        python -c "import x; print(...)"
        Observation:
        ['Doc', '__builtins__', ...]
    which then ran as bash and broke on the `[Doc,` lines.
    """
    text = (
        "Thought: probe the module\n"
        "Action: BASH\n"
        'Input: python -c "import annotated_doc; print(dir(annotated_doc))"\n\n'
        "Observation:\n['Doc', '__builtins__', 'main']\n"
    )
    _, action = parse_action(text)
    assert action.name == "BASH"
    assert "annotated_doc" in action.input
    assert "Observation" not in action.input
    assert "['Doc'" not in action.input


def test_parse_action_strips_trailing_xml_tool_call_artifacts():
    """charmbracelet/bubbletea on Sonnet had inputs ending in `</parameter>`.

    Sonnet sometimes slips into native tool-call XML mode and leaks the
    closing tag into the input field. Bash chokes on the syntax error.
    """
    text = "Thought: list modules\nAction: BASH\nInput: cd /workspace && cat go.mod</parameter>\n"
    _, action = parse_action(text)
    assert action.name == "BASH"
    assert action.input.endswith("go.mod")
    assert "</parameter>" not in action.input


def test_parse_action_strips_multiple_stacked_xml_artifacts():
    text = "Thought: x\nAction: BASH\nInput: ls /workspace</parameter></invoke></function_calls>\n"
    _, action = parse_action(text)
    assert action.input == "ls /workspace"


def test_parse_save_setup_tolerates_trailing_text():
    """gofiber/fiber on Sonnet emitted SAVE_SETUP JSON followed by extra text.

    raw_decode() should return the first complete JSON object and ignore
    everything after, instead of raising "Extra data: ..." like json.loads.
    """
    payload = _parse_save_setup(
        '{"rebuild_cmds": ["go mod download"], "test_cmds": ["go test ./..."]}'
        "\n\nNote: this should work."
    )
    assert payload["rebuild_cmds"] == ["go mod download"]
    assert payload["test_cmds"] == ["go test ./..."]


def test_parse_save_setup_handles_leading_prose():
    """Some models prepend a sentence before the JSON. Find the first `{`."""
    payload = _parse_save_setup(
        "Here is the setup JSON:\n"
        '{"rebuild_cmds": ["pip install -e ."], "test_cmds": ["python -m pytest"]}'
    )
    assert payload["test_cmds"] == ["python -m pytest"]
