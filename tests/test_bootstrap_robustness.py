"""v0.8.1 robustness plumbing: post-commit verify + budget enforcement.

No live Docker / live LLM in these tests — everything is mocked. The point
is to lock in the contract that the agent loop honors max_llm_spend_usd
and that the runner runs an authoritative verify on the committed image.
"""

from __future__ import annotations

from unittest import mock

from repo2rlenv.bootstrap import agent
from repo2rlenv.bootstrap import runner as runner_mod
from repo2rlenv.bootstrap.docker import ExecResult
from repo2rlenv.bootstrap.runner import _verify_committed_image

# ----------------------------------------------------------------------------
# Verify step: replay test_cmds in a fresh container from the committed image.
# ----------------------------------------------------------------------------


def _exec_result(exit_code: int, out: str = "", err: str = "") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=out, stderr=err, duration_sec=0.1)


def test_verify_empty_test_cmds_passes():
    ok, detail = _verify_committed_image("local/foo:abc", [], platform="linux/amd64")
    assert ok
    assert "skip" in detail.lower()


def test_verify_passes_on_pytest_exit_zero():
    with mock.patch.object(runner_mod, "_run", return_value=_exec_result(0, "5 passed in 0.4s")):
        ok, detail = _verify_committed_image(
            "local/foo:abc", ["python -m pytest -q"], platform="linux/amd64"
        )
    assert ok
    assert "exit=0" in detail


def test_verify_passes_on_pytest_exit_one_tests_failed():
    """pytest exit 1 = tests ran but some failed. That's an env-fine signal."""
    with mock.patch.object(runner_mod, "_run", return_value=_exec_result(1, "1 failed, 2 passed")):
        ok, _ = _verify_committed_image(
            "local/foo:abc", ["python -m pytest -q"], platform="linux/amd64"
        )
    assert ok


def test_verify_passes_on_pytest_exit_five_no_tests_collected():
    """pytest exit 5 = no tests collected. Still means the env runs."""
    with mock.patch.object(runner_mod, "_run", return_value=_exec_result(5, "no tests collected")):
        ok, _ = _verify_committed_image(
            "local/foo:abc", ["python -m pytest -q"], platform="linux/amd64"
        )
    assert ok


def test_verify_fails_on_env_level_error():
    """Exit 2/127 = pytest binary missing, env broken."""
    with mock.patch.object(
        runner_mod, "_run", return_value=_exec_result(127, err="pytest: not found")
    ):
        ok, detail = _verify_committed_image(
            "local/foo:abc", ["python -m pytest -q"], platform="linux/amd64"
        )
    assert not ok
    assert "exit=127" in detail


def test_verify_joins_multiple_test_cmds_with_and():
    """Multiple test_cmds must run as ONE shell so env-var exports carry over."""
    captured: list[list[str]] = []

    def fake_run(args, *, timeout, **kw):
        captured.append(list(args))
        return _exec_result(0)

    with mock.patch.object(runner_mod, "_run", side_effect=fake_run):
        _verify_committed_image(
            "local/foo:abc",
            ["export PATH=/opt/x/bin:$PATH", "python -m pytest --collect-only -q"],
            platform="linux/amd64",
        )

    assert len(captured) == 1, "must be a single docker run call"
    args = captured[0]
    assert "docker" in args[0] and "run" in args
    script = args[-1]
    assert "&&" in script
    assert script.startswith("export PATH=")


# ----------------------------------------------------------------------------
# Cost cap: agent loop must short-circuit when total_cost crosses max_spend_usd.
# ----------------------------------------------------------------------------


def test_agent_loop_short_circuits_when_cost_budget_hit():
    """First LLM call returns a high cost; loop must abort with success=False."""

    class FakeLLM:
        provider = "anthropic"
        model = "claude-sonnet-4-6"
        qualified_name = "anthropic/claude-sonnet-4-6"
        api_key_env = None
        endpoint = None
        timeout_sec = 60

    class FakeResponse:
        content = "Thought: looking\nAction: BASH\nInput: ls"
        cost_usd = 5.50  # above the 1.0 cap
        usage = None
        prompt_tokens = 100
        completion_tokens = 50

    fake_sandbox = mock.Mock()
    fake_sandbox.exec.return_value = _exec_result(0, "files...")

    with mock.patch.object(agent, "complete", return_value=FakeResponse()):
        outcome = agent.run_agent_loop(
            fake_sandbox,
            repo="owner/name",
            ref="a" * 40,
            language=runner_mod.LanguageHint.PYTHON,
            base_image="python:3.12-slim",
            llm=FakeLLM(),  # type: ignore[arg-type]
            max_iterations=20,
            max_seconds=600,
            max_spend_usd=1.0,
        )

    # The first call cost $5.50 which is above the $1 cap. The loop should
    # execute that turn (the cap check happens at the *start* of the loop),
    # then abort on the SECOND iteration when it re-checks total_cost.
    assert not outcome.success
    assert "cost budget exceeded" in outcome.reason
    assert outcome.iterations <= 2


def test_agent_loop_respects_max_iterations_with_no_cost_cap():
    """If max_spend_usd is None, loop runs until max_iterations or success."""

    class FakeLLM:
        provider = "anthropic"
        model = "claude-sonnet-4-6"
        qualified_name = "anthropic/claude-sonnet-4-6"
        api_key_env = None
        endpoint = None
        timeout_sec = 60

    class FakeResponse:
        content = "Thought: think\nAction: BASH\nInput: ls"
        cost_usd = 0.0
        usage = None
        prompt_tokens = 10
        completion_tokens = 5

    fake_sandbox = mock.Mock()
    fake_sandbox.exec.return_value = _exec_result(0, "files")

    with mock.patch.object(agent, "complete", return_value=FakeResponse()):
        outcome = agent.run_agent_loop(
            fake_sandbox,
            repo="owner/name",
            ref="a" * 40,
            language=runner_mod.LanguageHint.PYTHON,
            base_image="python:3.12-slim",
            llm=FakeLLM(),  # type: ignore[arg-type]
            max_iterations=3,
            max_seconds=600,
            max_spend_usd=None,
        )

    assert not outcome.success
    assert outcome.iterations == 3
    assert "max_iterations" in outcome.reason


# ----------------------------------------------------------------------------
# Temperature handling: newer reasoning models reject the param.
# ----------------------------------------------------------------------------


def test_temperature_suppressed_for_opus_4_7():
    from repo2rlenv.llm import _supports_temperature

    assert not _supports_temperature("claude-opus-4-7")
    assert _supports_temperature("claude-sonnet-4-6")


def test_temperature_suppressed_for_gpt_5_family():
    from repo2rlenv.llm import _supports_temperature

    assert not _supports_temperature("gpt-5.5")
    assert not _supports_temperature("gpt-5-5-2026-04-23")
    assert _supports_temperature("gpt-4o")


def test_temperature_suppressed_for_reasoning_models():
    from repo2rlenv.llm import _supports_temperature

    assert not _supports_temperature("o1-preview")
    assert not _supports_temperature("o3-mini")


def test_qwen_via_hf_router_supports_temperature():
    from repo2rlenv.llm import _supports_temperature

    assert _supports_temperature("Qwen/Qwen3-Coder-480B-A35B-Instruct:together")


# ----------------------------------------------------------------------------
# BootstrapResult: new verify fields surface from the runner.
# ----------------------------------------------------------------------------


def test_bootstrap_result_has_verify_fields():
    from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint

    result = BootstrapResult(
        image_digest="sha256:abc",
        image_tag="local/foo:abc",
        language=LanguageHint.PYTHON,
        repo="owner/name",
        ref="a" * 40,
        rebuild_cmds=["pip install -e ."],
        test_cmds=["python -m pytest"],
        smoke_passed=True,
        iterations=5,
        build_time_sec=120.0,
        llm_provider="anthropic/claude-sonnet-4-6",
    )
    assert hasattr(result, "verify_passed")
    assert hasattr(result, "verify_detail")
    # Defaults
    assert result.verify_passed is False
    assert result.verify_detail == ""
