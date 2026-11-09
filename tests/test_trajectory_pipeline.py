"""Offline tests (no Docker, no LLM): replaying a trajectory, mining repair candidates, config."""

from __future__ import annotations

from repo2rlenv.pipelines.trajectory.families.repair import Repair
from repo2rlenv.pipelines.trajectory.trajectory import Trajectory, VirtualFilesystem


def tool_step(step_id, tool, arguments, output, is_error=False):
    result = {"source_call_id": f"call{step_id}", "content": output}
    if is_error:
        result["extra"] = {"is_error": True}
    return {
        "step_id": step_id,
        "source": "agent",
        "message": "",
        "tool_calls": [
            {"tool_call_id": f"call{step_id}", "function_name": tool, "arguments": arguments}
        ],
        "observation": {"results": [result]},
    }


WRITE_BUGGY_CODE = tool_step(
    2, "Write", {"file_path": "/app/calc.py", "content": "def add(a, b):\n    return a - b\n"}, "ok"
)
WRITE_TEST = tool_step(
    3,
    "Write",
    {
        "file_path": "/app/test_calc.py",
        "content": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    },
    "ok",
)
RUN_TESTS_FAILING = tool_step(
    4,
    "Bash",
    {"command": "cd /app && python -m pytest -q"},
    "Exit code 1\nFAILED test_calc.py::test_add - assert -1 == 3",
    is_error=True,
)
FIX_CODE = tool_step(
    5, "Edit", {"file_path": "/app/calc.py", "old_string": "a - b", "new_string": "a + b"}, "ok"
)
RUN_TESTS_PASSING = tool_step(6, "Bash", {"command": "cd /app && python -m pytest -q"}, "1 passed")
USER_REQUEST = {
    "step_id": 1,
    "source": "user",
    "message": "<environment_context>cwd</environment_context>Write add()",
}


def trajectory(*steps):
    return Trajectory(
        {"schema_version": "ATIF-v1.7", "session_id": "demo", "steps": [USER_REQUEST, *steps]}
    )


def test_replay_gives_files_before_each_command():
    runs, filesystem = trajectory(
        WRITE_BUGGY_CODE, WRITE_TEST, RUN_TESTS_FAILING, FIX_CODE, RUN_TESTS_PASSING
    ).replay()
    assert [run.exit_code for run in runs] == [1, 0]
    assert runs[0].files_before["/app/calc.py"].endswith("a - b\n")
    assert filesystem.files["/app/calc.py"].endswith("a + b\n")


def test_user_messages_drop_injected_context():
    assert trajectory().user_messages() == [(1, "Write add()")]


def test_apply_patch_resolves_relative_paths():
    filesystem = VirtualFilesystem({"/work/a.py": "x = 1\ny = 2\n"})
    filesystem.apply_patch(
        "*** Begin Patch\n*** Update File: a.py\n@@\n-x = 1\n+x = 10\n y = 2\n*** End Patch",
        "/work",
    )
    assert filesystem.files["/work/a.py"] == "x = 10\ny = 2\n"


def test_repair_finds_the_failing_then_passing_check():
    (candidate,) = Repair().find_candidates(
        trajectory(WRITE_BUGGY_CODE, WRITE_TEST, RUN_TESTS_FAILING, FIX_CODE, RUN_TESTS_PASSING)
    )
    assert candidate.project_root == "/app" and candidate.changed_files == ["calc.py"]
    assert "a + b" in candidate.files_at_success["calc.py"]


def test_repair_ignores_fixes_that_only_change_tests():
    weaken_test = tool_step(
        5,
        "Edit",
        {"file_path": "/app/test_calc.py", "old_string": "== 3", "new_string": "== -1"},
        "ok",
    )
    assert (
        Repair().find_candidates(
            trajectory(
                WRITE_BUGGY_CODE, WRITE_TEST, RUN_TESTS_FAILING, weaken_test, RUN_TESTS_PASSING
            )
        )
        == []
    )


def test_trajectory_source_runs_natively():
    from repo2rlenv.spec.input import GenerationInput, TrajectorySource
    from repo2rlenv.spec.options import parse_options

    config = GenerationInput.model_validate(
        {
            "source": {"kind": "trajectory", "path": "trajectories", "format": "codex"},
            "pipeline": {"name": "trajectory_to_env"},
            "output": {"destination": "out", "org": "local", "dataset_name": "smoke"},
        }
    )
    assert isinstance(config.source, TrajectorySource) and config.repo is None
    assert parse_options("trajectory_to_env", {"families": "repair,qa"}).families == [
        "repair",
        "qa",
    ]


def test_options_configure_llm_and_docker(tmp_path, monkeypatch):
    from repo2rlenv.pipelines.trajectory import docker, llm
    from repo2rlenv.pipelines.trajectory.pipeline import TrajectoryPipeline
    from repo2rlenv.spec.input import GenerationInput
    from repo2rlenv.spec.options import parse_options

    for name in ("SPEC", "MODEL", "MAX_OUTPUT_TOKENS", "REASONING_EFFORT", "CACHE_DIR"):
        monkeypatch.setattr(llm, name, getattr(llm, name))
    for name in ("PLATFORM", "CONTAINER_MEMORY"):
        monkeypatch.setattr(docker, name, getattr(docker, name))
    config = GenerationInput.model_validate(
        {
            "source": {"kind": "trajectory", "path": str(tmp_path)},
            "pipeline": {"name": "trajectory_to_env"},
            "llm": {"provider": "huggingface", "model": "org/model:provider"},
            "output": {"destination": "out", "org": "local", "dataset_name": "smoke"},
        }
    )
    options = parse_options(
        "trajectory_to_env",
        {
            "max_llm_tokens": "48000",
            "reasoning_effort": "low",
            "llm_cache_dir": str(tmp_path / "cache"),
            "platform": "native",
            "container_memory": "2g",
        },
    )
    TrajectoryPipeline(config, options)
    assert (llm.MODEL, llm.MAX_OUTPUT_TOKENS, llm.REASONING_EFFORT) == (
        "huggingface/org/model:provider",
        48000,
        "low",
    )
    assert tmp_path / "cache" == llm.CACHE_DIR
    assert (docker.PLATFORM, docker.CONTAINER_MEMORY) == ("native", "2g")


def test_llm_spend_cap_stops_new_calls(tmp_path, monkeypatch):
    import pytest
    from pydantic import BaseModel

    from repo2rlenv.llm import LLMResponse
    from repo2rlenv.pipelines.trajectory import llm
    from repo2rlenv.spec.input import LLMSpec

    class Answer(BaseModel):
        n: int

    for name in ("SPEC", "MODEL", "CACHE_DIR", "BUDGET_USD", "SPENT_USD"):
        monkeypatch.setattr(llm, name, getattr(llm, name))
    calls = []

    def complete(spec, **kwargs):
        calls.append(kwargs)
        return LLMResponse(content='{"n": 1}', usage=None, cost_usd=0.06)

    monkeypatch.setattr(llm, "complete", complete)
    llm.configure(LLMSpec(provider="x", model="y"), cache_dir=tmp_path, budget_usd=0.1)

    llm.ask("s", "first", Answer)
    llm.ask("s", "second", Answer)  # $0.06 spent, still under $0.10
    assert llm.ask("s", "first", Answer).n == 1  # cached: free, allowed past the cap
    with pytest.raises(llm.BudgetExceeded):
        llm.ask("s", "third", Answer)
    assert len(calls) == 2 and llm.spent_usd() == pytest.approx(0.12)


def test_spend_option_defaults_to_5_and_zero_is_unlimited():
    from repo2rlenv.spec.options import parse_options

    assert parse_options("trajectory_to_env", {}).max_llm_spend_usd == 5.0
    assert parse_options("trajectory_to_env", {"max_llm_spend_usd": "0"}).max_llm_spend_usd is None
