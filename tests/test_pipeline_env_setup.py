"""Tests for `pipelines.env_setup` — the pipeline itself.

Every test here runs with no Docker, no LLM, and no network: `_sandbox_factory`,
`distill_setup_recipe`, and `_dry_run_gates` are all monkeypatched to fakes,
and `_run_oracle_gate` is monkeypatched to a fixed float so the (real, harbor-
CLI-shelling) oracle gate never runs. `_FakeSandbox` and `_bootstrap` come
from `tests/_env_setup_fakes.py` (shared with `tests/test_setup_recipe.py`).
"""

from __future__ import annotations

import pytest

from tests._env_setup_fakes import _bootstrap, _FakeSandbox


def _gen_input(repo="pallets/click", ref="HEAD", *, with_llm=True):
    """GenerationInput requires repo + pipeline + output (input.py:197-206)."""
    from repo2rlenv.spec.input import (
        GenerationInput,
        LLMSpec,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )

    return GenerationInput(
        repo=RepoSpec(url=repo, ref=ref),
        pipeline=PipelineSpec(name=PipelineName.ENV_SETUP),
        output=OutputSpec(destination="local", org="test", dataset_name="env-setup-test"),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6") if with_llm else None,
    )


def test_env_setup_rejects_local_source(tmp_path):
    """A file:// source cannot serve a `docker build` clone. The capability
    makes the cli.py pre-flight reject it; the __init__ check is the guard for
    non-CLI callers, which docs/reference/API.md shows exactly.
    """
    from repo2rlenv.pipelines.env_setup import EnvSetupPipeline
    from repo2rlenv.sources import Capability
    from repo2rlenv.spec.options import EnvSetupOptions

    assert Capability.REMOTE_CLONE in EnvSetupPipeline.required_capabilities
    with pytest.raises(ValueError, match=r"REMOTE_CLONE|local"):
        EnvSetupPipeline(_gen_input(repo=f"file://{tmp_path}"), EnvSetupOptions())


def test_env_setup_classvars():
    from repo2rlenv.pipelines.env_setup import EnvSetupPipeline
    from repo2rlenv.spec.input import PipelineName

    assert EnvSetupPipeline.name == PipelineName.ENV_SETUP
    assert EnvSetupPipeline.requires_bootstrap is True
    assert EnvSetupPipeline.experimental is True
    assert EnvSetupPipeline.supported_languages is None
    # reward_kinds is a metadata key, not a class attribute — no pipeline
    # declares it as one.
    assert not hasattr(EnvSetupPipeline, "reward_kinds")


def test_env_setup_is_registered():
    from repo2rlenv.pipelines import PIPELINES
    from repo2rlenv.pipelines.env_setup import EnvSetupPipeline
    from repo2rlenv.spec.input import PipelineName

    assert PIPELINES[PipelineName.ENV_SETUP] is EnvSetupPipeline


def _run_pipeline(
    monkeypatch,
    tmp_path,
    *,
    bootstrap,
    options=None,
    recipe=None,
    fprime_reward=1.0,
    fprime_status="ok",
):
    """Drive run() with the bootstrap, recipe module, and container faked.

    `_run_oracle_gate` is pinned to a constant 1.0 pass so a test that leaves
    `oracle_gate=True` (the option default) never shells out to the real
    `harbor` CLI — which IS on PATH in this environment and would otherwise
    attempt (and fail-fast against) a live Docker daemon. Pinning it keeps
    every test in this module Docker/network-free regardless of what's
    installed on the machine running the suite.
    """
    from repo2rlenv.pipelines import env_setup as mod
    from repo2rlenv.pipelines._setup_recipe import RecipeOutcome
    from repo2rlenv.spec.options import EnvSetupOptions

    monkeypatch.setattr(mod, "_sandbox_factory", lambda *a, **k: _FakeSandbox([]))
    monkeypatch.setattr(
        mod,
        "distill_setup_recipe",
        lambda **kw: (
            recipe
            or RecipeOutcome(
                "set -euo pipefail\npip install -e .\n",
                # 5 PASSED, matching EnvSetupOptions.min_target_tests' default
                # floor of 5 — tests that don't care about the floor still need
                # to clear it to reach the code path they actually exercise.
                "tests/test_a.py::test_v PASSED\ntests/test_a.py::test_w PASSED\n"
                "tests/test_a.py::test_x PASSED\ntests/test_a.py::test_y PASSED\n"
                "tests/test_a.py::test_z PASSED",
                1,
                0.02,
                3.0,
                9.0,
                None,
                [],
            )
        ),
    )
    monkeypatch.setattr(mod, "_dry_run_gates", lambda **kw: (fprime_reward, fprime_status))
    monkeypatch.setattr(mod, "_has_lockfile", lambda sandbox: False)
    monkeypatch.setattr(
        mod, "_resolve_package_names_in_container", lambda sandbox, language: ("click", "click")
    )
    monkeypatch.setattr(mod.EnvSetupPipeline, "_run_oracle_gate", lambda self, *a, **k: 1.0)
    pipeline = mod.EnvSetupPipeline(
        _gen_input(), options or EnvSetupOptions(oracle_gate=False), bootstrap
    )
    return pipeline.run(tmp_path)


def test_user_dockerfile_bootstrap_is_skipped(monkeypatch, tmp_path):
    """Asserted WITHOUT relying on the verify_passed=False default: that
    default is a coincidence that currently points the right way. The real
    reason outlives it — that path gives language=UNKNOWN next to a
    user-authored FROM we never parse, so the base-image agreement cannot be
    established at all.
    """
    bs = _bootstrap(verify_passed=True, smoke_passed=True, extra={"source": "user_dockerfile"})
    result = _run_pipeline(monkeypatch, tmp_path, bootstrap=bs)
    assert result.emitted == 0
    assert result.skip_reasons == {"bootstrap_source_unsupported": 1}


def test_bootstrap_failed_on_verify_passed_false(monkeypatch, tmp_path):
    """verify_passed replays test_cmds in a FRESH container from the committed
    tag — it catches state the agent set in the live shell that docker commit
    dropped. A recipe distilled from that transcript will not reproduce.
    """
    bs = _bootstrap(smoke_passed=True, verify_passed=False)
    result = _run_pipeline(monkeypatch, tmp_path, bootstrap=bs)
    assert result.skip_reasons == {"bootstrap_failed": 1}


def test_base_image_mismatch_skips(monkeypatch, tmp_path):
    """A skip, not an AssertionError: an AssertionError inside run() lands in
    the generic build_failed bucket and loses the diagnosis.
    """
    bs = _bootstrap(dockerfile_reconstruction="FROM alpine:3.20\nRUN true\n")
    result = _run_pipeline(monkeypatch, tmp_path, bootstrap=bs)
    assert result.skip_reasons == {"base_image_mismatch": 1}


def test_blank_test_cmds_dropped(monkeypatch, tmp_path):
    bs = _bootstrap(test_cmds=["| head -50", "2>&1", "  "])
    result = _run_pipeline(monkeypatch, tmp_path, bootstrap=bs)
    assert result.skip_reasons == {"no_runnable_test_cmds": 1}


def test_runner_undetectable_skips(monkeypatch, tmp_path):
    """Never a baked empty --runner, and never a misleading too_few_tests:
    detect_runner("") is `unknown`, parse_logs("unknown", ...) is {}, and the
    verifier would silently fall to a binary exit-code reward.
    """
    bs = _bootstrap(test_cmds=["make check"])
    result = _run_pipeline(monkeypatch, tmp_path, bootstrap=bs)
    assert result.skip_reasons == {"runner_undetectable": 1}


def test_too_few_tests_skips(monkeypatch, tmp_path):
    from repo2rlenv.pipelines._setup_recipe import RecipeOutcome
    from repo2rlenv.spec.options import EnvSetupOptions

    recipe = RecipeOutcome(
        "set -e\n",
        "a.py::t1 PASSED\na.py::t2 PASSED\na.py::t3 PASSED",
        1,
        0.0,
        1.0,
        1.0,
        None,
        [],
    )
    result = _run_pipeline(
        monkeypatch,
        tmp_path,
        bootstrap=_bootstrap(),
        recipe=recipe,
        options=EnvSetupOptions(min_target_tests=5, oracle_gate=False),
    )
    assert result.skip_reasons == {"too_few_tests": 1}


def test_target_floor_applied_before_truncation(monkeypatch, tmp_path):
    """min=5, max=3, 10 passing => emitted with 3 F2P, NOT skipped. A legal
    config now that the floor/cap validator is gone, which is what makes the
    ordering observable at all.
    """
    import json

    from repo2rlenv.pipelines._setup_recipe import RecipeOutcome
    from repo2rlenv.spec.options import EnvSetupOptions

    log = "\n".join(f"a.py::t{i} PASSED" for i in range(10))
    recipe = RecipeOutcome("set -e\n", log, 1, 0.0, 1.0, 1.0, None, [])
    result = _run_pipeline(
        monkeypatch,
        tmp_path,
        bootstrap=_bootstrap(),
        recipe=recipe,
        options=EnvSetupOptions(min_target_tests=5, max_target_tests=3, oracle_gate=False),
    )
    assert result.emitted == 1
    f2p = json.loads((next(tmp_path.iterdir()) / "tests" / "f2p.json").read_text())
    assert len(f2p) == 3


def test_target_set_uses_verifier_parser(monkeypatch, tmp_path):
    """From parse_logs, not a local reimplementation; SKIPPED never enters
    f2p.json — an agent that installs nothing still "passes" a skipped test.
    """
    import json

    from repo2rlenv.pipelines._setup_recipe import RecipeOutcome

    log = (
        "a.py::t1 PASSED\na.py::t2 SKIPPED\na.py::t3 PASSED\n"
        "a.py::t4 PASSED\na.py::t5 PASSED\na.py::t6 PASSED\n"
    )
    recipe = RecipeOutcome("set -e\n", log, 1, 0.0, 1.0, 1.0, None, [])
    result = _run_pipeline(monkeypatch, tmp_path, bootstrap=_bootstrap(), recipe=recipe)
    assert result.emitted == 1
    f2p = json.loads((next(tmp_path.iterdir()) / "tests" / "f2p.json").read_text())
    assert "a.py::t2" not in f2p
    assert len(f2p) == 5


def test_gates_unverified_when_probe_ladder_exhausts(monkeypatch, tmp_path):
    result = _run_pipeline(
        monkeypatch,
        tmp_path,
        bootstrap=_bootstrap(),
        fprime_reward=0.0,
        fprime_status="package_not_from_source",
    )
    assert result.skip_reasons == {"gates_unverified": 1}
    # And no half-written task directory left behind.
    assert list(tmp_path.iterdir()) == []


def test_oracle_gate_raise_leaves_no_task_dir(monkeypatch, tmp_path):
    """A raise anywhere in the write_harbor_task / _run_oracle_gate region
    (e.g. `shutil.copytree` onto a full disk, outside `_run_oracle_gate`'s own
    narrow `except (TimeoutExpired, OSError)`) must not leave an unverified
    task directory on disk, and must still show up as a recorded skip rather
    than a directory nobody accounts for.
    """
    from repo2rlenv.pipelines import env_setup as mod
    from repo2rlenv.pipelines._setup_recipe import RecipeOutcome
    from repo2rlenv.spec.options import EnvSetupOptions

    monkeypatch.setattr(mod, "_sandbox_factory", lambda *a, **k: _FakeSandbox([]))
    monkeypatch.setattr(
        mod,
        "distill_setup_recipe",
        lambda **kw: RecipeOutcome(
            "set -euo pipefail\npip install -e .\n",
            "tests/test_a.py::test_v PASSED\ntests/test_a.py::test_w PASSED\n"
            "tests/test_a.py::test_x PASSED\ntests/test_a.py::test_y PASSED\n"
            "tests/test_a.py::test_z PASSED",
            1,
            0.02,
            3.0,
            9.0,
            None,
            [],
        ),
    )
    monkeypatch.setattr(mod, "_dry_run_gates", lambda **kw: (1.0, "ok"))
    monkeypatch.setattr(mod, "_has_lockfile", lambda sandbox: False)
    monkeypatch.setattr(
        mod, "_resolve_package_names_in_container", lambda sandbox, language: ("click", "click")
    )

    def _boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod.EnvSetupPipeline, "_run_oracle_gate", _boom)

    pipeline = mod.EnvSetupPipeline(
        _gen_input(), EnvSetupOptions(oracle_gate=True, emit_solution=True), _bootstrap()
    )
    result = pipeline.run(tmp_path)

    assert result.emitted == 0
    assert result.skip_reasons == {"build_failed": 1}
    assert list(tmp_path.iterdir()) == []


def test_reward_kinds_are_spec_defined(monkeypatch, tmp_path):
    import tomllib

    result = _run_pipeline(monkeypatch, tmp_path, bootstrap=_bootstrap())
    assert result.emitted == 1
    toml = tomllib.loads((next(tmp_path.iterdir()) / "task.toml").read_text())
    meta = toml["metadata"]["repo2env"]
    assert meta["reward_kinds"] == ["test_execution"]
    assert meta["env_setup"]["reward_granularity"] == "graded"
    assert meta["env_setup"]["oracle"] == "recipe"


def test_normalized_cmds_used_everywhere(monkeypatch, tmp_path):
    import tomllib

    bs = _bootstrap(test_cmds=["python -m pytest -v | head -50"])
    _run_pipeline(monkeypatch, tmp_path, bootstrap=bs)
    task_dir = next(tmp_path.iterdir())
    toml = tomllib.loads((task_dir / "task.toml").read_text())
    test_cmd = toml["metadata"]["repo2env"]["env_setup"]["test_cmd"]
    assert test_cmd in (task_dir / "tests" / "test.sh").read_text()
    assert "head -50" not in test_cmd


def test_emit_solution_false_governs_emission_not_generation(monkeypatch, tmp_path):
    """Distillation, the green run, the F2P parse, and F' all still run —
    there is no other route to the F2P set. The pipeline simply does not write
    solution/, and records oracle="none".
    """
    import json
    import tomllib

    from repo2rlenv.spec.options import EnvSetupOptions

    result = _run_pipeline(
        monkeypatch,
        tmp_path,
        bootstrap=_bootstrap(),
        options=EnvSetupOptions(emit_solution=False, oracle_gate=True),
    )
    assert result.emitted == 1
    task_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    assert not (task_dir / "solution").exists()
    assert json.loads((task_dir / "tests" / "f2p.json").read_text())
    meta = tomllib.loads((task_dir / "task.toml").read_text())["metadata"]["repo2env"]
    assert meta["env_setup"]["oracle"] == "none"


def test_extra_refs_dedup_on_resolved_sha(monkeypatch, tmp_path):
    """RepoSpec.ref defaults to "HEAD", so refs=["<sha-of-HEAD>"] would
    otherwise bootstrap the same tree twice and emit two task directories with
    the same id. Dedup is on the resolved SHA, not the ref string.
    """
    from repo2rlenv.pipelines import env_setup as mod
    from repo2rlenv.spec.options import EnvSetupOptions

    monkeypatch.setattr(mod, "ensure_bootstrap", lambda **kw: _bootstrap())
    result = _run_pipeline(
        monkeypatch,
        tmp_path,
        bootstrap=_bootstrap(),
        options=EnvSetupOptions(refs=["a" * 40], oracle_gate=False),
    )
    assert result.candidates == 2
    assert result.emitted == 1
    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 1


def test_primary_ref_is_always_first_and_always_a_candidate():
    """Anything else wastes the bootstrap cmd_generate already paid for — the
    most expensive single step in this pipeline.
    """
    from repo2rlenv.pipelines.env_setup import EnvSetupPipeline
    from repo2rlenv.spec.options import EnvSetupOptions

    p = EnvSetupPipeline(_gen_input(ref="v1.2.3"), EnvSetupOptions(refs=["v1.0.0", "v1.2.3"]), None)
    assert p._candidate_refs() == ["v1.2.3", "v1.0.0"]
