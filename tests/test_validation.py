"""`repo2rlenv validate --deep` / `--oracle` — task-aware static checks.

Fixtures are built with the real emitter + pipeline builders, so "no findings
on emitted output" guards against false positives on tasks we actually ship.
"""

from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path
from typing import get_args

import pytest
import tomli_w
from rich.console import Console

from repo2rlenv.cli import main
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.pipelines._env_guard import egress_guard_compose
from repo2rlenv.pipelines.code_instruct import build_code_instruct_dockerfile
from repo2rlenv.pipelines.pr_diff import (
    _pr_diff_aux_files,
    build_pr_diff_environment_dockerfile,
    build_pr_diff_eval_script,
)
from repo2rlenv.pipelines.pr_runtime import (
    _runtime_aux_files,
    build_environment_dockerfile,
    build_eval_script,
)
from repo2rlenv.registry.integration import ReproMode
from repo2rlenv.ui import console
from repo2rlenv.validation import REPRO_MODES, Finding, validate_task

BASE = "a" * 40
PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
F2P = ["tests/test_x.py::test_fixed[a b]"]
P2P = ["tests/test_x.py::test_other"]


def _task(pipeline: str, **kwargs) -> HarborTask:
    return HarborTask(
        name=f"demo__repo-{pipeline}",
        org="myorg",
        description="example",
        instruction="# Issue\n\nfix the bug",
        oracle_diff=PATCH,
        repo2env={"pipeline": pipeline, "repo": "demo/repo", **kwargs.pop("repo2env", {})},
        **kwargs,
    )


def _pr_diff_lite(tmp_path: Path) -> Path:
    return write_harbor_task(_task("pr_diff"), tmp_path)


def _pr_diff_env(tmp_path: Path) -> Path:
    task = _task(
        "pr_diff",
        environment_dockerfile=build_pr_diff_environment_dockerfile(
            repo_url="https://github.com/demo/repo.git",
            base_commit=BASE,
        ),
        test_script=build_pr_diff_eval_script(base_commit=BASE),
        aux_files=_pr_diff_aux_files(oracle_diff=PATCH, instruction="fix the bug"),
    )
    return write_harbor_task(task, tmp_path)


def _pr_runtime_graded(tmp_path: Path) -> Path:
    task = _task(
        "pr_runtime",
        repo2env={
            "reward_kinds": ["test_execution", "diff_similarity"],
            "pr_runtime": {"fail_to_pass": F2P, "pass_to_pass": P2P, "reward_mode": "graded"},
        },
        environment_dockerfile=build_environment_dockerfile(
            "local/r2e-bootstrap/demo__repo:abc", BASE
        ),
        test_script=build_eval_script(BASE, "", ["pytest -q"], fail_to_pass=F2P, pass_to_pass=P2P),
        aux_files={
            **_runtime_aux_files(F2P, P2P),
            "environment/docker-compose.yaml": egress_guard_compose(),
        },
    )
    return write_harbor_task(task, tmp_path)


def _code_instruct(tmp_path: Path) -> Path:
    task = _task(
        "code_instruct",
        repo2env={
            "reward_kinds": ["test_execution"],
            "code_instruct": {"test_filename": "test_task_module.py"},
        },
        environment_dockerfile=build_code_instruct_dockerfile("local/r2e-bootstrap/demo:abc"),
        test_script="#!/bin/bash\ncd /workspace && pytest -q /tests/test_task_module.py\n",
        aux_files={"tests/test_task_module.py": "def test_ok():\n    assert True\n"},
    )
    return write_harbor_task(task, tmp_path)


def _load(task_dir: Path) -> dict:
    return tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))


def _rewrite(task_dir: Path, mutate) -> dict:
    data = _load(task_dir)
    mutate(data)
    (task_dir / "task.toml").write_text(tomli_w.dumps(data), encoding="utf-8")
    return data


def _errors(findings: list[Finding]) -> set[str]:
    return {f.path for f in findings if f.severity == "error"}


def _warnings(findings: list[Finding]) -> set[str]:
    return {f.path for f in findings if f.severity == "warning"}


@pytest.mark.parametrize(
    "pipeline,recipe", [("terminal_synth", "tmax"), ("equivalence_tests", "r2e")]
)
def test_recipe_oracle_can_be_a_script_without_a_patch(tmp_path, pipeline, recipe):
    task_dir = _pr_runtime_graded(tmp_path)
    data = _rewrite(
        task_dir,
        lambda d: d["metadata"]["repo2env"].update(pipeline=pipeline, recipe=recipe),
    )
    (task_dir / "solution/patch.diff").unlink()
    (task_dir / "solution/solve.sh").write_text(
        "#!/bin/bash\nprintf 'reference output' > /workspace/output\n"
    )
    assert _errors(validate_task(task_dir, data, oracle=True)) == set()
    (task_dir / "solution/solve.sh").unlink()
    assert "solution/solve.sh" in _errors(validate_task(task_dir, data, oracle=True))


def test_recipe_oracle_still_checks_a_supplied_patch(tmp_path):
    task_dir = _pr_runtime_graded(tmp_path)
    data = _rewrite(task_dir, lambda d: d["metadata"]["repo2env"].update(recipe="swe-gen"))
    (task_dir / "solution/patch.diff").write_text("not a patch")
    assert "solution/patch.diff" in _errors(validate_task(task_dir, data, oracle=True))


# ----------------------------------------------------------------------------
# No false positives on what the emitter actually writes
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("build", [_pr_diff_lite, _pr_diff_env, _pr_runtime_graded, _code_instruct])
def test_emitted_tasks_have_no_findings(tmp_path: Path, build):
    task_dir = build(tmp_path)
    assert validate_task(task_dir, _load(task_dir), oracle=True) == []


def test_emitted_tasks_pass_without_exec_bits(tmp_path: Path):
    """Harbor runs `chmod +x` itself — mode 0644 scripts must not fail."""
    task_dir = _pr_runtime_graded(tmp_path)
    for script in ("tests/test.sh", "solution/solve.sh"):
        (task_dir / script).chmod(0o644)
    assert validate_task(task_dir, _load(task_dir), oracle=True) == []


def test_repro_modes_track_registry_literal():
    assert frozenset(get_args(ReproMode)) == REPRO_MODES


# ----------------------------------------------------------------------------
# The issue #100 reproduction
# ----------------------------------------------------------------------------

ISSUE_100_TOML = """\
version = "1.0"

[task]
name = "review/broken"

[metadata.repo2env]
reward_kinds = ["test_execution"]

[metadata.repo2env.reproducibility]
mode = "nonsense"
"""


def test_issue_100_reproduction_reports_every_problem(tmp_path: Path):
    (tmp_path / "task.toml").write_text(ISSUE_100_TOML, encoding="utf-8")
    findings = validate_task(tmp_path, tomllib.loads(ISSUE_100_TOML))
    assert _errors(findings) == {"instruction.md", "tests/test.sh", "environment", "task.toml"}
    assert any("mode must be one of" in f.message for f in findings)


def test_cli_default_validate_is_unchanged(tmp_path: Path):
    (tmp_path / "task.toml").write_text(ISSUE_100_TOML, encoding="utf-8")
    assert main(["validate", str(tmp_path)]) == 0


def test_cli_deep_fails_issue_100_reproduction(tmp_path: Path):
    (tmp_path / "task.toml").write_text(ISSUE_100_TOML, encoding="utf-8")
    assert main(["validate", str(tmp_path), "--deep"]) == 1


def test_cli_deep_output_keeps_toml_table_names(tmp_path: Path, monkeypatch):
    """`[environment]` etc. are Rich markup tags — they must be escaped, not eaten."""
    buf = io.StringIO()
    monkeypatch.setattr(console, "console", Console(file=buf, width=300, no_color=True))
    (tmp_path / "task.toml").write_text(ISSUE_100_TOML, encoding="utf-8")
    main(["validate", str(tmp_path), "--deep"])
    out = buf.getvalue()
    assert "[environment].docker_image" in out
    assert "[metadata.repo2env.reproducibility].mode must be one of" in out
    assert "review/broken: tests/test.sh: missing" in out


def test_cli_oracle_implies_deep(tmp_path: Path):
    (tmp_path / "task.toml").write_text(ISSUE_100_TOML, encoding="utf-8")
    assert main(["validate", str(tmp_path), "--oracle"]) == 1


def test_cli_oracle_passes_emitted_dataset(tmp_path: Path):
    for build in (_pr_diff_lite, _pr_diff_env, _pr_runtime_graded, _code_instruct):
        build(tmp_path / build.__name__)  # both pr_diff fixtures share a task name
    assert main(["validate", str(tmp_path), "--oracle"]) == 0


# ----------------------------------------------------------------------------
# Harbor layout variants
# ----------------------------------------------------------------------------


def test_blank_instruction_is_an_error(tmp_path: Path):
    task_dir = _pr_diff_lite(tmp_path)
    (task_dir / "instruction.md").write_text("  \n", encoding="utf-8")
    assert _errors(validate_task(task_dir, _load(task_dir))) == {"instruction.md"}


def test_text_only_pr_diff_needs_patch_even_without_oracle(tmp_path: Path):
    task_dir = _pr_diff_lite(tmp_path)
    (task_dir / "solution" / "patch.diff").unlink()
    assert _errors(validate_task(task_dir, _load(task_dir))) == {"solution/patch.diff"}


def test_legacy_multi_component_reward_kind_is_diff_similarity(tmp_path: Path):
    """Published v0.8.3 pr_diff datasets carry the pre-rename kind."""
    task_dir = _pr_diff_lite(tmp_path)
    data = _rewrite(
        task_dir,
        lambda d: d["metadata"]["repo2env"].update(
            reward_kinds=["diff_similarity_multi_component"]
        ),
    )
    assert validate_task(task_dir, data) == []
    (task_dir / "solution" / "patch.diff").unlink()
    assert _errors(validate_task(task_dir, data)) == {"solution/patch.diff"}


def test_prebuilt_docker_image_satisfies_environment(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    (task_dir / "environment" / "Dockerfile").unlink()
    data = _rewrite(task_dir, lambda d: d.update(environment={"docker_image": "python:3.12"}))
    assert validate_task(task_dir, data) == []


def test_compose_only_environment_is_accepted(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    (task_dir / "environment" / "Dockerfile").unlink()
    (task_dir / "environment" / "docker-compose.yaml").write_text("services: {}\n")
    assert validate_task(task_dir, _load(task_dir)) == []


def test_missing_environment_definition_on_runtime_task(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    (task_dir / "environment" / "Dockerfile").unlink()
    assert _errors(validate_task(task_dir, _load(task_dir))) == {"environment"}


def test_empty_environment_dir_makes_lite_task_runnable(tmp_path: Path):
    task_dir = _pr_diff_lite(tmp_path)
    (task_dir / "environment").mkdir()
    assert _errors(validate_task(task_dir, _load(task_dir))) == {"environment", "tests/test.sh"}


def test_windows_task_uses_bat_scripts(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    (task_dir / "tests" / "test.sh").rename(task_dir / "tests" / "test.bat")
    (task_dir / "solution" / "solve.sh").rename(task_dir / "solution" / "solve.bat")
    data = _rewrite(task_dir, lambda d: d.update(environment={"os": "windows"}))
    assert validate_task(task_dir, data, oracle=True) == []


def test_linux_task_does_not_accept_bat_test_script(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    (task_dir / "tests" / "test.sh").rename(task_dir / "tests" / "test.bat")
    assert _errors(validate_task(task_dir, _load(task_dir))) == {"tests/test.sh"}


def test_non_r2e_task_only_checks_what_exists(tmp_path: Path):
    (tmp_path / "instruction.md").write_text("do the thing", encoding="utf-8")
    data = {"version": "1.0", "task": {"name": "org/plain"}}
    assert validate_task(tmp_path, data, oracle=True) == []

    (tmp_path / "environment").mkdir()
    (tmp_path / "tests").mkdir()
    assert _errors(validate_task(tmp_path, data)) == {"environment", "tests/test.sh"}


def test_multi_step_task_skips_layout_checks(tmp_path: Path):
    data = {"version": "1.0", "task": {"name": "org/steps"}, "steps": [{"name": "one"}]}
    findings = validate_task(tmp_path, data)
    assert _errors(findings) == set()
    assert _warnings(findings) == {"task.toml"}


def test_unknown_pipeline_and_reward_kind_warn(tmp_path: Path):
    task_dir = _pr_diff_lite(tmp_path)

    def mutate(d):
        d["metadata"]["repo2env"]["pipeline"] = "unregistered_example_pipeline"
        d["metadata"]["repo2env"]["reward_kinds"] = ["diff_similarity", "llm_judge"]

    findings = validate_task(task_dir, _rewrite(task_dir, mutate))
    assert _errors(findings) == set()
    assert len([f for f in findings if f.severity == "warning"]) == 2


# ----------------------------------------------------------------------------
# Known Repo2RLEnv verifier assets
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel,content",
    [
        ("tests/f2p.json", "{not json"),
        ("tests/f2p.json", json.dumps({"tests": F2P})),
        ("tests/f2p.json", "[]"),
        ("tests/p2p.json", json.dumps([1, 2])),
        ("tests/verifier.py", "def broken(:\n"),
        ("tests/verifier.py", ""),
    ],
)
def test_graded_verifier_assets_must_parse(tmp_path: Path, rel: str, content: str):
    task_dir = _pr_runtime_graded(tmp_path)
    (task_dir / rel).write_text(content, encoding="utf-8")
    assert _errors(validate_task(task_dir, _load(task_dir))) == {rel}


def test_graded_verifier_assets_must_exist(tmp_path: Path):
    task_dir = _pr_runtime_graded(tmp_path)
    for name in ("verifier.py", "f2p.json", "p2p.json"):
        (task_dir / "tests" / name).unlink()
    assert _errors(validate_task(task_dir, _load(task_dir))) == {
        "tests/verifier.py",
        "tests/f2p.json",
        "tests/p2p.json",
    }


def test_binary_reward_runtime_task_needs_no_verifier_assets(tmp_path: Path):
    """No F2P oracle (e.g. --skip-validation) → exit-code reward, no aux files."""
    task_dir = _pr_runtime_graded(tmp_path)
    for name in ("verifier.py", "f2p.json", "p2p.json"):
        (task_dir / "tests" / name).unlink()
    data = _rewrite(task_dir, lambda d: d["metadata"]["repo2env"]["pr_runtime"].pop("fail_to_pass"))
    assert validate_task(task_dir, data) == []


def test_f2p_mismatch_with_metadata_warns(tmp_path: Path):
    task_dir = _pr_runtime_graded(tmp_path)
    (task_dir / "tests" / "f2p.json").write_text(json.dumps(["other::test"]), encoding="utf-8")
    findings = validate_task(task_dir, _load(task_dir))
    assert _errors(findings) == set()
    assert _warnings(findings) == {"tests/f2p.json"}


def test_code_instruct_test_file_must_exist(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    (task_dir / "tests" / "test_task_module.py").unlink()
    assert _errors(validate_task(task_dir, _load(task_dir))) == {"tests/test_task_module.py"}


def test_code_instruct_test_filename_cannot_escape_tests_dir(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    data = _rewrite(
        task_dir,
        lambda d: d["metadata"]["repo2env"]["code_instruct"].update(test_filename="../task.toml"),
    )
    assert _errors(validate_task(task_dir, data)) == {"task.toml"}


# ----------------------------------------------------------------------------
# [metadata.repo2env.reproducibility]
# ----------------------------------------------------------------------------


def _set_repro(task_dir: Path, repro: dict) -> dict:
    return _rewrite(task_dir, lambda d: d["metadata"]["repo2env"].update(reproducibility=repro))


def test_legacy_task_without_reproducibility_is_valid(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)

    def mutate(d):
        d["metadata"]["repo2env"].pop("reproducibility")
        d["metadata"]["repo2env"]["spec_version"] = "0.1.0"

    assert validate_task(task_dir, _rewrite(task_dir, mutate)) == []


def test_v020_sandbox_task_without_reproducibility_warns(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    data = _rewrite(task_dir, lambda d: d["metadata"]["repo2env"].pop("reproducibility"))
    findings = validate_task(task_dir, data)
    assert _errors(findings) == set()
    assert _warnings(findings) == {"task.toml"}


@pytest.mark.parametrize(
    "repro",
    [
        {"mode": "nonsense"},
        {"mode": ["registry"]},  # unhashable: must report, not crash
        {},
        {"mode": "registry"},  # image_ref required
        {"mode": "registry", "image_ref": "  "},
        {"mode": "local_only", "image_visibility": "world"},
        {"mode": "local_only", "image_ref": 42},
        {"mode": "inline_dockerfile", "inline_recipe_source": "vibes"},
        {"mode": "inline_dockerfile", "inline_recipe_sha256": "sha256:nothex"},
        {"mode": "inline_dockerfile", "inline_recipe_lines": -1},
        {"mode": "inline_dockerfile", "inline_recipe_lines": True},
    ],
)
def test_invalid_reproducibility_metadata(tmp_path: Path, repro: dict):
    task_dir = _code_instruct(tmp_path)
    assert _errors(validate_task(task_dir, _set_repro(task_dir, repro))) == {"task.toml"}


def test_valid_registry_and_inline_modes(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    digest = "ghcr.io/demo/r2e-bootstrap-demo@sha256:" + "b" * 64
    dockerfile = task_dir / "environment" / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace("local/r2e-bootstrap/demo:abc", digest),
        encoding="utf-8",
    )
    registry = {
        "mode": "registry",
        "image_ref": digest,
        "image_tag": "ghcr.io/demo/r2e-bootstrap-demo:abc",
        "image_visibility": "public",
        "pushed_at": "2026-09-14T00:00:00+00:00",
        "pushed_by": "demo",
    }
    assert validate_task(task_dir, _set_repro(task_dir, registry)) == []

    inline = {
        "mode": "inline_dockerfile",
        "inline_recipe_sha256": "sha256:" + "c" * 64,
        "inline_recipe_lines": 12,
        "inline_recipe_source": "agent_replay",
        "fallback_reason": "no working registry credentials",
    }
    assert validate_task(task_dir, _set_repro(task_dir, inline)) == []


def test_registry_from_line_mismatch_warns(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    findings = validate_task(
        task_dir, _set_repro(task_dir, {"mode": "registry", "image_ref": "ghcr.io/x/y@sha256:1"})
    )
    assert _errors(findings) == set()
    assert _warnings(findings) == {"environment/Dockerfile"}


def test_inline_mode_requires_dockerfile(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    (task_dir / "environment" / "Dockerfile").unlink()
    (task_dir / "environment" / "docker-compose.yaml").write_text("services: {}\n")
    data = _set_repro(task_dir, {"mode": "inline_dockerfile"})
    assert _errors(validate_task(task_dir, data)) == {"environment/Dockerfile"}


# ----------------------------------------------------------------------------
# --oracle
# ----------------------------------------------------------------------------


def test_oracle_checks_are_opt_in(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    for rel in ("solution/patch.diff", "solution/solve.sh"):
        (task_dir / rel).unlink()
    assert validate_task(task_dir, _load(task_dir)) == []
    assert _errors(validate_task(task_dir, _load(task_dir), oracle=True)) == {
        "solution/patch.diff",
        "solution/solve.sh",
    }


def test_oracle_patch_must_look_like_a_diff(tmp_path: Path):
    task_dir = _code_instruct(tmp_path)
    (task_dir / "solution" / "patch.diff").write_text("just prose\n", encoding="utf-8")
    assert _errors(validate_task(task_dir, _load(task_dir), oracle=True)) == {"solution/patch.diff"}


def test_oracle_search_replace_patch_skips_diff_header_check(tmp_path: Path):
    task_dir = _pr_diff_lite(tmp_path)
    (task_dir / "solution" / "patch.diff").write_text(
        "x.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n", encoding="utf-8"
    )
    data = _rewrite(
        task_dir,
        lambda d: d["metadata"]["repo2env"].update(pr_diff={"diff_format": "search_replace"}),
    )
    assert validate_task(task_dir, data, oracle=True) == []
