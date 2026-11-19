"""Bootstrap must save test commands whose output the log parsers can read."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from repo2rlenv.bootstrap import agent
from repo2rlenv.bootstrap import parseability as pa
from repo2rlenv.bootstrap.docker import ExecResult
from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint

# What moment's `scripts/test.js` prints: a summary line, no per-test results.
QUNIT_SUMMARY = (
    "\nGlobal summary:\n17 tests, 154 assertions, 0 failed tests, 0 failed assertions, 28ms\n"
)
PYTEST_VERBOSE = (
    "tests/test_a.py::test_one PASSED                                    [ 50%]\n"
    "tests/test_a.py::test_two PASSED                                    [100%]\n"
)


def _result(out: str, code: int = 0) -> ExecResult:
    return ExecResult(exit_code=code, stdout=out, stderr="", duration_sec=0.1)


def _sandbox(out: str, code: int = 0):
    box = mock.Mock()
    box.exec.return_value = _result(out, code)
    return box


def test_summary_only_output_parses_to_zero_tests():
    cmds = ["cd /workspace && node scripts/test.js"]
    assert pa.count_parsed_tests(cmds, QUNIT_SUMMARY, language="node") == 0


def test_probe_counts_per_test_lines_and_runs_normalized_commands():
    box = _sandbox(PYTEST_VERBOSE)
    parsed, code, _ = pa.probe_parseable(box, ["pytest -q"], language="python")
    assert parsed == 2
    assert code == 0
    # -q is dropped and -v added, exactly as the runtime pipelines run it.
    assert box.exec.call_args.args[0] == "pytest -v"


def test_probe_reports_zero_for_unparseable_runner():
    parsed, _, tail = pa.probe_parseable(
        _sandbox(QUNIT_SUMMARY), ["node scripts/test.js"], language="node"
    )
    assert parsed == 0
    assert "Global summary" in tail


def test_rejection_message_names_formats_and_shows_output():
    message = pa.rejection_message(0, "Global summary: 17 tests")
    assert "SAVE_SETUP rejected" in message
    assert "pytest -v" in message
    assert "Global summary: 17 tests" in message


class _LLM:
    provider = "anthropic"
    model = "claude-sonnet-4-6"
    qualified_name = "anthropic/claude-sonnet-4-6"
    api_key_env = None
    endpoint = None
    timeout_sec = 60


def _reply(test_cmd: str):
    return SimpleNamespace(
        content=(
            "Thought: done\nAction: SAVE_SETUP\nInput: "
            f'{{"rebuild_cmds": [], "test_cmds": ["{test_cmd}"], "summary": "ok"}}'
        ),
        cost_usd=0.0,
        usage=None,
        prompt_tokens=1,
        completion_tokens=1,
    )


def _run(box, replies, *, require):
    with mock.patch.object(agent, "complete", side_effect=replies):
        return agent.run_agent_loop(
            box,
            repo="owner/name",
            ref="a" * 40,
            language=LanguageHint.NODE,
            base_image="node:22-slim",
            llm=_LLM(),  # type: ignore[arg-type]
            max_iterations=len(replies),
            require_parseable_tests=require,
        )


def test_agent_rejects_unparseable_test_cmds_then_accepts_a_parseable_one():
    box = mock.Mock()
    box.exec.side_effect = [_result(QUNIT_SUMMARY), _result(PYTEST_VERBOSE)]
    outcome = _run(box, [_reply("node scripts/test.js"), _reply("pytest -v")], require=True)

    assert outcome.success
    assert outcome.test_cmds == ["pytest -v"]
    assert outcome.tests_parsed == 2
    assert outcome.iterations == 2


def test_agent_gives_up_when_every_saved_command_is_unparseable():
    box = _sandbox(QUNIT_SUMMARY)
    outcome = _run(box, [_reply("node scripts/test.js")] * 3, require=True)

    assert not outcome.success
    assert "max_iterations" in outcome.reason


def test_agent_gate_off_keeps_previous_behaviour():
    box = _sandbox(QUNIT_SUMMARY)
    outcome = _run(box, [_reply("node scripts/test.js")], require=False)

    assert outcome.success
    assert outcome.tests_parsed is None
    box.exec.assert_not_called()


def test_old_cache_without_tests_parsed_loads_as_unmeasured(tmp_path):
    from repo2rlenv.bootstrap import cache

    result = BootstrapResult(
        image_digest="sha256:x",
        image_tag="local/x:1",
        language=LanguageHint.PYTHON,
        repo="o/n",
        ref="a" * 40,
        rebuild_cmds=[],
        test_cmds=["pytest -v"],
        smoke_passed=True,
        iterations=1,
        build_time_sec=1.0,
        llm_provider="anthropic/x",
    )
    cache.save(result, tmp_path)
    slot = cache.cache_key("o/n", "a" * 40, tmp_path)
    import json

    data = json.loads((slot / "bootstrap.json").read_text())
    data.pop("tests_parsed")
    (slot / "bootstrap.json").write_text(json.dumps(data))

    assert cache.load("o/n", "a" * 40, tmp_path).tests_parsed is None


def test_pr_runtime_fails_fast_when_bootstrap_parsed_no_tests():
    from repo2rlenv.pipelines.pr_runtime import PRRuntimePipeline
    from repo2rlenv.spec.options import PRRuntimeOptions

    pipeline = PRRuntimePipeline.__new__(PRRuntimePipeline)
    pipeline.options = PRRuntimeOptions()
    pipeline._supplied_bootstrap = SimpleNamespace(
        ref="a" * 40, tests_parsed=0, test_cmds=["node scripts/test.js"]
    )

    with pytest.raises(RuntimeError, match="no parseable per-test results"):
        pipeline._bootstrap_for_pr(SimpleNamespace(number=1, base_sha="a" * 40))
