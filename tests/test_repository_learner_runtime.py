"""Exercise emitted learner startup using temporary shell fixtures, never task code."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from repo2rlenv.pipelines.recipes.repository.export import export_repository_task, repository_build
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile


def exported_task(tmp_path: Path, *, venv: bool = True):
    base = tmp_path / "source"
    (base / "src").mkdir(parents=True)
    (base / "src/core.py").write_text("value = 1\n")
    options = PythonRepositoryProfile(
        source_paths=["src"], test_paths=["tests"], use_system_site_packages=venv
    )
    task = export_repository_task(
        base=base,
        defective={"src/core.py": b"value = 0\n"},
        reference={"src/core.py": b"value = 1\n"},
        options=options,
        instruction="Restore the public value.",
        destination=tmp_path / "tasks",
        name="learner-runtime-fixture",
        org="test",
        contrast={"FAIL_TO_PASS": ["test_value"], "PASS_TO_PASS": []},
        metadata={"recipe": "tasksmith", "recipe_version": "1"},
    )
    return task, options


def shell_fixture(tmp_path: Path):
    learner_home = tmp_path / "learner home"
    learner_home.mkdir()
    venv = tmp_path / "task runtime"
    base_bin = tmp_path / "base bin"
    for directory, value in ((base_bin, "base"), (venv / "bin", "task-venv")):
        directory.mkdir(parents=True)
        for name in ("python", "python3"):
            executable = directory / name
            executable.write_text(f"#!/bin/sh\nprintf '%s\\n' {value}\n")
            executable.chmod(0o755)
    # A child-only HOME keeps the test away from developer/system login files.
    env = {"HOME": str(learner_home), "PATH": "/usr/bin:/bin", "BASE_BIN": str(base_bin)}
    return learner_home, venv, env


def startup_result(learner_home: Path, env: dict[str, str]):
    selected = next(
        (
            name
            for name in (".bash_profile", ".bash_login", ".profile")
            if (learner_home / name).exists()
        ),
        None,
    )
    # Model a system-profile PATH reset, then execute the actual emitted user
    # startup. Do not source this machine's /etc/profile or real home files.
    command = 'export PATH="$BASE_BIN:/usr/bin:/bin"; '
    if selected:
        command += f'. "$HOME/{selected}"; '
    command += (
        'printf "%s\\n" "${STARTUP_KIND-none}" "${BASHRC_RAN-no}"; '
        'python; python3; printf "%s\\n" "${VIRTUAL_ENV-none}"'
    )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return result.stdout.splitlines()


@pytest.mark.parametrize("selected", [None, ".profile", ".bash_login", ".bash_profile"])
def test_emitted_login_preserves_startup_and_restores_runtime_last(tmp_path, selected):
    task, _ = exported_task(tmp_path)
    learner_home, venv, env = shell_fixture(tmp_path)
    startup_names = [".bash_profile", ".bash_login", ".profile"]
    originals = {}
    if selected:
        (learner_home / ".bashrc").write_text('export BASHRC_RAN=yes\nexport PATH="$BASE_BIN"\n')
        for name in startup_names[startup_names.index(selected) :]:
            text = (
                f"export STARTUP_KIND={shlex.quote(name)}\n"
                '. "$HOME/.bashrc"\n'
                "return 0\n"
                "export STARTUP_KIND=incorrectly-executed-after-return\n"
            )
            (learner_home / name).write_text(text)
            originals[name] = text
    before = startup_result(learner_home, env)
    assert before[2:4] == ["base", "base"]
    command = next(
        line.removeprefix("RUN ")
        for line in (task / "environment/Dockerfile").read_text().splitlines()
        if line.startswith("RUN sh -c ")
    )
    argv = shlex.split(command)
    assert argv[:2] == ["sh", "-c"]
    assert argv[3:6] == ["--", "/home/learner", "/opt/tasksmith-venv"]
    # Execute the exact generated installer on temporary home/runtime paths.
    # The Docker-only chown remains outside this command and is never run here.
    subprocess.run(
        ["/bin/sh", "-c", argv[2], "--", str(learner_home), str(venv)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    after = startup_result(learner_home, env)
    assert after == [
        selected or "none",
        "yes" if selected else "no",
        "task-venv",
        "task-venv",
        str(venv),
    ]
    for name, contents in originals.items():
        preserved = ".repo2rlenv-original-bash-profile" if name == ".bash_profile" else name
        assert (learner_home / preserved).read_text() == contents


@pytest.mark.parametrize("venv", [False, True])
def test_runtime_fix_is_only_a_learner_layer_and_public_hint(tmp_path, venv):
    task, options = exported_task(tmp_path, venv=venv)
    learner = (task / "environment/Dockerfile").read_text()
    verifier = (task / "tests/Dockerfile").read_text()
    prefix = repository_build(options)
    assert learner.startswith(prefix) and verifier.startswith(prefix)
    assert (".bash_profile" in learner) is venv
    assert ".bash_profile" not in verifier
    assert ("Use `/opt/tasksmith-venv/bin/python`" in (task / "instruction.md").read_text()) is venv
    expected = "/opt/tasksmith-venv/bin/python" if venv else "/usr/local/bin/python"
    assert (
        task / "tests/test.sh"
    ).read_text() == f"#!/bin/sh\nset -eu\nexec {expected} -I /tests/grade.py\n"
    assert (task / "environment/source/src/core.py").read_text() == "value = 0\n"
    assert (task / "solution/reference/src/core.py").read_text() == "value = 1\n"
