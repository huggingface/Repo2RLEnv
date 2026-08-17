"""Tests for `pipelines._setup_recipe` — recipe distillation + the retry loop.

Every test here runs with no Docker, no network, and no real LLM call: the
sandbox is `_FakeSandbox` (tests/_env_setup_fakes.py) and `complete` is
monkeypatched to a scripted stand-in. `EnvSetupSandbox` (the Docker half) has
no unit test by design; it is thin `subprocess` plumbing exercised by the E2E
task.
"""

from __future__ import annotations

from repo2rlenv.pipelines._setup_recipe import extract_script
from tests._env_setup_fakes import _bootstrap, _FakeSandbox


def test_recipe_parse_from_transcript():
    fenced = (
        "Here is the recipe:\n"
        "```bash\n"
        "#!/bin/bash\nset -euo pipefail\npython -m pip install -e .\n"
        "```\n"
        "Hope that helps!\n"
    )
    assert extract_script(fenced) == (
        "#!/bin/bash\nset -euo pipefail\npython -m pip install -e .\n"
    )
    # Unfenced but script-shaped: accept.
    assert extract_script("set -euo pipefail\npip install -e .\n") is not None
    # Prose with no commands: reject rather than shipping an empty recipe.
    assert extract_script("I could not determine how to install this project.") is None
    assert extract_script("") is None


def test_recipe_source_falls_back_to_reconstruction(tmp_path):
    from repo2rlenv.pipelines._setup_recipe import recipe_source_from_bootstrap

    transcript = tmp_path / "t.json"
    transcript.write_text(
        '[{"action": "BASH", "command": "pip install -e .", "exit_code": 0, "output": "ok"}]'
    )
    src = recipe_source_from_bootstrap(_bootstrap(transcript_path=transcript))
    assert "pip install -e ." in src

    # No transcript: fall back to reconstruction + rebuild_cmds. The only
    # surviving justification is cache entries predating transcript linking,
    # so expect this rung cold on fresh runs.
    src = recipe_source_from_bootstrap(
        _bootstrap(
            transcript_path=None,
            dockerfile_reconstruction="FROM python:3.12-slim\nRUN pip install -e .\n",
            rebuild_cmds=["pip install -e ."],
        )
    )
    assert "pip install -e ." in src

    # Neither: no source at all.
    assert (
        recipe_source_from_bootstrap(
            _bootstrap(transcript_path=None, dockerfile_reconstruction="", rebuild_cmds=[])
        )
        is None
    )


def _options(**kw):
    from repo2rlenv.spec.options import EnvSetupOptions

    return EnvSetupOptions(**kw)


def _llm_spec():
    # LLMSpec lives in spec/input.py, NOT in llm.py.
    from repo2rlenv.spec.input import LLMSpec

    return LLMSpec(provider="anthropic", model="claude-sonnet-4-6")


def _distill(monkeypatch, *, responses, results, options=None, **kw):
    """Run distill_setup_recipe with a scripted LLM and a fake sandbox.

    Returns (outcome, prompts, sandbox).
    """
    from repo2rlenv.bootstrap.spec import LanguageHint
    from repo2rlenv.llm import LLMResponse
    from repo2rlenv.pipelines import _setup_recipe

    prompts = []

    def fake_complete(spec, *, system=None, user, max_tokens=1024, temperature=0.7):
        prompts.append(user)
        return LLMResponse(content=responses[len(prompts) - 1], cost_usd=0.01)

    monkeypatch.setattr(_setup_recipe, "complete", fake_complete)
    sandbox = _FakeSandbox(results)
    outcome = _setup_recipe.distill_setup_recipe(
        bootstrap=kw.pop("bootstrap", None)
        or _bootstrap(
            transcript_path=None, dockerfile_reconstruction="FROM x\nRUN pip install -e .\n"
        ),
        test_cmds=["python -m pytest -v"],
        base_image="python:3.12-slim",
        language=LanguageHint.PYTHON,
        llm_spec=_llm_spec(),
        options=options or _options(),
        sandbox=sandbox,
        **kw,
    )
    return outcome, prompts, sandbox


def test_recipe_green_and_clean_on_first_attempt(monkeypatch):
    outcome, prompts, _sandbox = _distill(
        monkeypatch,
        responses=["```bash\nset -euo pipefail\npip install -e .\n```"],
        # setup.sh -> 0, test run -> 0 (with a parseable log), git diff -> 0
        results=[(0, ""), (0, "tests/test_a.py::test_x PASSED"), (0, "")],
    )
    assert outcome.skip_reason is None
    assert "pip install -e ." in outcome.setup_sh
    assert outcome.attempts == 1
    assert outcome.cost_usd == 0.01
    assert "PASSED" in outcome.log
    assert len(prompts) == 1


def test_recipe_retry_carries_stderr(monkeypatch):
    outcome, prompts, _ = _distill(
        monkeypatch,
        responses=[
            "```bash\npip install -e .\n```",
            "```bash\napt-get update && apt-get install -y gcc\npip install -e .\n```",
        ],
        results=[
            (0, ""),
            (1, "ERROR: gcc not found"),
            (0, ""),  # attempt 1: red
            (0, ""),
            (0, "tests/a.py::t PASSED"),
            (0, ""),  # attempt 2: green + clean
        ],
    )
    assert outcome.skip_reason is None
    assert outcome.attempts == 2
    assert len(prompts) == 2
    # The retry must carry BOTH the failed script and the captured output.
    assert "pip install -e ." in prompts[1]
    assert "gcc not found" in prompts[1]


def test_recipe_unverified_after_exhausting_attempts(monkeypatch, tmp_path):
    outcome, prompts, _ = _distill(
        monkeypatch,
        responses=["```bash\npip install -e .\n```"] * 3,
        results=[(0, ""), (1, "boom"), (0, "")] * 3,
        options=_options(max_recipe_attempts=3),
        debug_dir=tmp_path / "dbg",
    )
    assert outcome.skip_reason == "recipe_unverified"
    assert outcome.setup_sh is None
    assert outcome.attempts == 3
    assert len(prompts) == 3
    dumps = list((tmp_path / "dbg").glob("*"))
    assert dumps, "exhaustion must dump attempts to .debug_skips/"


def test_recipe_rejected_when_it_edits_tracked_files(monkeypatch, tmp_path):
    """Green is necessary but not sufficient. Gate 1/2 restores the tracked
    tree before grading, so a recipe depending on an in-tree edit scores 0 on
    its own task. Catching it here names the fault and feeds a retry that can
    fix it — F' would catch it too, but only as an opaque gates_unverified.
    """
    outcome, prompts, _ = _distill(
        monkeypatch,
        responses=["```bash\nsed -i s/x/y/ setup.py\npip install -e .\n```"] * 3,
        # setup.sh 0, tests 0 (green!), git diff --quiet 1 (DIRTY) — every time
        results=[(0, ""), (0, "a::b PASSED"), (1, " M setup.py")] * 3,
        options=_options(max_recipe_attempts=3),
        debug_dir=tmp_path / "dbg",
    )
    assert outcome.skip_reason == "recipe_edits_tracked_files"
    assert len(prompts) == 3
    assert "do not modify files tracked in the repository" in prompts[1].lower()


def test_recipe_no_source_skips_without_calling_the_llm(monkeypatch):
    outcome, prompts, _ = _distill(
        monkeypatch,
        responses=[],
        results=[],
        bootstrap=_bootstrap(transcript_path=None, dockerfile_reconstruction="", rebuild_cmds=[]),
    )
    assert outcome.skip_reason == "no_recipe_source"
    assert prompts == []


def test_recipe_unverified_when_last_attempt_is_unparseable_after_dirty_run(monkeypatch, tmp_path):
    """recipe_edits_tracked_files must name the TRUE fault of the last attempt.

    Attempt 1 is a real, green-but-tracked-dirty run. Attempt 2 (the final
    one) is an unparseable LLM response, which tells us nothing about
    tracked-file dirtiness. The terminal skip_reason must be
    recipe_unverified, not a stale recipe_edits_tracked_files carried over
    from attempt 1.
    """
    outcome, prompts, _ = _distill(
        monkeypatch,
        responses=[
            "```bash\nsed -i s/x/y/ setup.py\npip install -e .\n```",
            "I could not determine how to install this project.",
        ],
        # attempt 1: setup.sh 0, tests 0 (green!), git diff --quiet 1 (DIRTY)
        results=[(0, ""), (0, "a::b PASSED"), (1, " M setup.py")],
        options=_options(max_recipe_attempts=2),
        debug_dir=tmp_path / "dbg",
    )
    assert outcome.skip_reason == "recipe_unverified"
    assert outcome.attempts == 2
    assert len(prompts) == 2
    assert outcome.history[0].tracked_dirty is True
    assert outcome.history[1].tracked_dirty is False


def test_env_setup_sandbox_exec_survives_timeout(monkeypatch):
    """A hung `docker exec` must degrade to a failed ExecResult, not raise.

    `recipe_verify_timeout_sec` defaults to 1800s and a hanging install is
    exactly the failure the retry loop exists to catch — an uncaught
    subprocess.TimeoutExpired would crash the whole env_setup run instead of
    letting distill_setup_recipe record recipe_unverified.
    """
    import subprocess

    from repo2rlenv.pipelines._setup_recipe import EnvSetupSandbox

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    sandbox = EnvSetupSandbox("deadbeef", "r2e/envsetup:test")

    result = sandbox.exec("sleep 9999", timeout=5)

    assert result.exit_code == 124
    assert not result.ok
    assert "timeout" in result.stderr


def test_recipe_captures_with_xtrace_off(monkeypatch):
    """The generation-time half of the capture-shape contract: step F must
    capture identically to gate 1, or the baked F2P ids and the graded ids
    disagree for jest.
    """
    _, _, sandbox = _distill(
        monkeypatch,
        responses=["```bash\npip install -e .\n```"],
        results=[(0, ""), (0, "a::b PASSED"), (0, "")],
    )
    capture_script = sandbox.scripts[1]
    assert "set +x" in capture_script
    assert "set -x" not in capture_script.split("set +x")[0]
