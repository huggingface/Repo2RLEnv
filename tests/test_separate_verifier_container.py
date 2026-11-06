"""Run the generated separate-verifier test.sh in a real container.

Opt-in (R2E_DOCKER_TESTS=1): builds a small python:3.12 image with pytest and
replays what Harbor does in separate mode: the agent's /workspace (or, for a
single-file task, its task_module.py) replaces the image's copy, /tests is
uploaded, and test.sh runs as root. Each case is a submission.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repo2rlenv.pipelines._eval_script import (
    build_single_file_eval_script,
    single_file_verifier_files,
)
from repo2rlenv.pipelines.pr_runtime import (
    _runtime_aux_files,
    build_eval_script,
    separate_verifier_files,
    stash_pristine,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("R2E_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="set R2E_DOCKER_TESTS=1 to build and run the verifier container",
)

IMAGE = "r2e-separate-verifier-test:latest"
BUGGY = "def clamp(x, lo=0, hi=1):\n    return x\n"
FIXED = "def clamp(x, lo=0, hi=1):\n    return max(lo, min(hi, x))\n"
EXISTING_TEST = (
    "from pkg.clamp import clamp\n\n\ndef test_inside():\n    assert clamp(0.5) == 0.5\n"
)
HIDDEN_TEST = (
    "from pkg.clamp import clamp\n\n\n"
    "def test_low():\n    assert clamp(-5) == 0\n\n\n"
    "def test_high():\n    assert clamp(7) == 1\n\n\n"
    "def test_custom():\n    assert clamp(9, 2, 4) == 4\n"
)
F2P = [
    "tests/test_fix.py::test_custom",
    "tests/test_fix.py::test_high",
    "tests/test_fix.py::test_low",
]
P2P = ["tests/test_clamp.py::test_inside"]
CMD = "python -m pytest -v -p no:cacheprovider tests/test_clamp.py tests/test_fix.py"

SETSID_WRITER = (
    FIXED
    + "import subprocess\n"
    + "subprocess.Popen(['setsid', 'sh', '-c', "
    + "'sleep 3; echo 1.0 > /logs/verifier/reward.txt; echo pwn > /tests/f2p.json'])\n"
)


# Per-run limit for this small suite; real tasks derive it from validation runs.
RUN_TIMEOUT = 20
PYPROJECT = (
    '[project]\nname = "pkg"\nversion = "1.0"\n\n'
    '[tool.pytest.ini_options]\naddopts = "-ra"\nmarkers = [\n  "slow: slow tests",\n]\n'
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _build_task(root: Path, image: str, base_image: str, setup: str = "") -> Path:
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "clamp.py").write_text(BUGGY)
    (repo / "tests" / "test_clamp.py").write_text(EXISTING_TEST)
    (repo / ".gitignore").write_text(".venv/\n")
    (repo / "pyproject.toml").write_text(PYPROJECT)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tests" / "test_fix.py").write_text(HIDDEN_TEST)
    _git(repo, "add", "-N", "tests/test_fix.py")
    test_patch = _git(repo, "diff", "--", "tests/test_fix.py")
    (repo / "tests" / "test_fix.py").unlink()
    # The image ships a virtualenv the agent must not be able to replace.
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "marker").write_text("image\n")

    tests = root / "tests"
    tests.mkdir()
    script = build_eval_script(
        base,
        test_patch,
        [CMD],
        language="python",
        fail_to_pass=F2P,
        pass_to_pass=P2P,
        run_timeout=RUN_TIMEOUT,
    )
    files = {"tests/test.sh": script, **_runtime_aux_files(F2P, P2P)}
    files.update(separate_verifier_files())
    for relative, content in files.items():
        (root / relative).write_text(content)
    (root / "Dockerfile").write_text(
        f"FROM {base_image}\n{setup}"
        "RUN pip install --no-cache-dir pytest==8.3.5\n"
        "COPY repo /workspace\n"
        "WORKDIR /workspace\n" + stash_pristine()
    )
    subprocess.run(["docker", "build", "-q", "-t", image, str(root)], check=True)
    root.joinpath("image").write_text(image)
    return root


@pytest.fixture(scope="module")
def task(tmp_path_factory):
    return _build_task(tmp_path_factory.mktemp("task"), IMAGE, "python:3.12")


@pytest.fixture(scope="module")
def task_py310(tmp_path_factory):
    # No tomllib, and -I -S hides any tomli: the guard reads TOML itself.
    return _build_task(
        tmp_path_factory.mktemp("task310"),
        "r2e-separate-verifier-test-py310:latest",
        "python:3.10-slim",
        "RUN apt-get update && apt-get install -y --no-install-recommends git procps"
        " && rm -rf /var/lib/apt/lists/*\n",
    )


@pytest.fixture(scope="module")
def task_unsafe(tmp_path_factory):
    # A world-writable __pycache__ on root's own import path: the test user could
    # plant bytecode that root's verifier would load.
    return _build_task(
        tmp_path_factory.mktemp("task_unsafe"),
        "r2e-separate-verifier-test-unsafe:latest",
        "python:3.12",
        "RUN mkdir -p /usr/local/lib/python3.12/json/__pycache__"
        " && chmod 777 /usr/local/lib/python3.12/json/__pycache__\n",
    )


def _grade(
    task: Path,
    tmp_path: Path,
    edits: dict[str, str],
    *,
    sparse: dict[str, int] | None = None,
    docker_args: tuple[str, ...] = (),
) -> dict:
    agent = tmp_path / "agent"
    shutil.copytree(task / "repo", agent, symlinks=True)
    for relative, content in edits.items():
        target = agent / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    # Sparse files are made inside the container, so nothing large is copied here.
    make_sparse = "".join(
        f"truncate -s {size} /workspace/{name} && " for name, size in (sparse or {}).items()
    )
    logs = tmp_path / "logs"
    logs.mkdir()
    # Harbor separate mode: empty /workspace, upload the agent's copy and /tests.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            *docker_args,
            "-v",
            f"{agent}:/agent:ro",
            "-v",
            f"{task / 'tests'}:/tests-src:ro",
            "-v",
            f"{logs}:/logs/verifier",
            (task / "image").read_text(),
            "bash",
            "-c",
            "find /workspace -mindepth 1 -delete && cp -a /agent/. /workspace/ && "
            + make_sparse
            + "cp -a /tests-src /tests && bash /tests/test.sh > /logs/verifier/stdout.txt 2>&1; "
            "sleep 4; ipcs -m -c > /logs/verifier/ipcs.txt 2>&1; "
            # Modes as the container sees them (a macOS host's bind mount may not).
            "stat -c '%a %n' /logs/verifier /logs/verifier/* > /tmp/modes.txt 2>&1; "
            "mv /tmp/modes.txt /logs/verifier/modes.txt",
        ],
        check=True,
        timeout=600,
    )
    details = json.loads((logs / "reward-details.json").read_text())
    reward = logs / "reward.txt"
    details["reward_txt"] = float(reward.read_text()) if reward.exists() else None
    return details


def test_oracle_scores_one(task, tmp_path):
    details = _grade(task, tmp_path, {"pkg/clamp.py": FIXED})
    assert details["reward_txt"] == 1.0, details
    assert details["evaluation_status"] == "graded"


def test_grading_outputs_are_readable_once_graded(task, tmp_path):
    _grade(task, tmp_path, {"pkg/clamp.py": FIXED})
    modes = dict(
        reversed(line.split(" ", 1))
        for line in (tmp_path / "logs" / "modes.txt").read_text().splitlines()
    )
    assert modes["/logs/verifier"] == "755"
    for name in ("reward-details.json", "submission.json", "test_output.log"):
        assert modes[f"/logs/verifier/{name}"] == "644", (name, modes)


def test_an_image_root_cannot_trust_is_ungraded(task_unsafe, tmp_path):
    details = _grade(task_unsafe, tmp_path, {"pkg/clamp.py": FIXED})
    assert details["reward_txt"] is None, details
    assert details["parse_status"] == "unsafe_image"
    assert details["evaluation_status"] == "ungraded"


def test_no_op_scores_zero(task, tmp_path):
    details = _grade(task, tmp_path, {})
    assert details["reward_txt"] == 0.0
    assert details["evaluation_status"] == "graded"


def test_agent_virtualenv_does_not_reach_grading(task, tmp_path):
    details = _grade(
        task,
        tmp_path,
        {"pkg/clamp.py": FIXED, ".venv/bin/marker": "agent\n", ".venv/bin/python": "exit 0\n"},
    )
    assert details["reward_txt"] == 1.0


def test_conftest_forgery_is_reset(task, tmp_path):
    conftest = (
        "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n    outcome.get_result().outcome = 'passed'\n"
    )
    details = _grade(task, tmp_path, {"tests/conftest.py": conftest, "conftest.py": conftest})
    assert details["reward_txt"] == 0.0


def test_leftover_process_cannot_rewrite_the_reward(task, tmp_path):
    details = _grade(task, tmp_path, {"pkg/clamp.py": SETSID_WRITER})
    # The fix is real, so 1.0 is right; the point is that nothing is rewritten
    # after grading (the container sleeps past the writer's delay).
    assert details["reward_txt"] == 1.0
    assert (tmp_path / "logs" / "reward.txt").read_text().strip() == "1.000000"


def test_a_hanging_submission_scores_zero(task, tmp_path):
    details = _grade(task, tmp_path, {"pkg/clamp.py": "import time\ntime.sleep(600)\n" + FIXED})
    assert details["reward_txt"] == 0.0, details
    assert details["parse_status"] == "test_timeout"
    assert details["evaluation_status"] == "graded"


def test_a_tree_too_deep_for_paths_is_still_cleaned_up(task, tmp_path):
    # Review finding: a /tmp chain past PATH_MAX made cleanup fail, which used
    # to leave the run ungraded. The launcher now removes it from a short path.
    deep = (
        "import os\nos.chdir('/tmp')\n"
        "for _ in range(30):\n    os.mkdir('d' * 200)\n    os.chdir('d' * 200)\n"
    )
    details = _grade(task, tmp_path, {"pkg/clamp.py": FIXED + deep})
    assert details["reward_txt"] == 1.0, details
    assert details["evaluation_status"] == "graded"


def test_sysv_shared_memory_does_not_outlive_the_run(task, tmp_path):
    shm = (
        "import ctypes\n"
        "ctypes.CDLL(None).shmget(0, 4096, 0o1600)  # IPC_PRIVATE, IPC_CREAT | 0600\n"
    )
    details = _grade(task, tmp_path, {"pkg/clamp.py": FIXED + shm})
    assert details["reward_txt"] == 1.0, details
    assert "20001" not in (tmp_path / "logs" / "ipcs.txt").read_text()


def test_the_test_user_cannot_list_the_logs(task, tmp_path):
    # The import fails, and the run scores 0, if /logs/verifier is readable.
    probe = (
        "import os\ntry:\n    os.listdir('/logs/verifier')\n"
        "except PermissionError:\n    pass\nelse:\n    raise RuntimeError('logs readable')\n"
    )
    details = _grade(task, tmp_path, {"pkg/clamp.py": FIXED + probe})
    assert details["reward_txt"] == 1.0, details


def test_a_file_bigger_than_memory_is_not_read(task, tmp_path):
    # Review finding: the rebuild read every candidate file whole, so a stray
    # file larger than the verifier's memory was an OOM kill, i.e. ungraded.
    details = _grade(
        task,
        tmp_path,
        {"pkg/clamp.py": FIXED},
        sparse={"stray.bin": 3 << 30},
        docker_args=("--memory", "512m"),
    )
    assert details["reward_txt"] == 1.0, details
    assert details["evaluation_status"] == "graded"


def test_a_huge_tracked_file_is_a_graded_zero_without_being_read(task, tmp_path):
    details = _grade(
        task, tmp_path, {}, sparse={"pkg/clamp.py": 3 << 30}, docker_args=("--memory", "512m")
    )
    assert details["reward_txt"] == 0.0, details
    assert details["parse_status"] == "submission_too_large"


def test_log_lines_that_stall_a_regex_still_grade(task, tmp_path):
    # Review finding: a few long lines made the verifier's pytest regex run for
    # minutes, ending in Harbor's ungraded timeout.
    flood = "print('tests/t.py::a' + ' ' * 200000 + 'x')\n" * 20
    details = _grade(task, tmp_path, {"pkg/clamp.py": FIXED + flood})
    assert details["reward_txt"] == 1.0, details
    assert details["evaluation_status"] == "graded"


def test_a_file_flood_does_not_stall_grading(task, tmp_path):
    # Review finding: deleting what the tests left took longer than the tests.
    # The verifier runs once in a discarded container and no longer deletes it.
    flood = (
        "import os, time\nos.makedirs('/tmp/flood', exist_ok=True)\n"
        "for i in range(200000):\n    open(f'/tmp/flood/{i}', 'w').close()\n"
    )
    details = _grade(task, tmp_path, {"pkg/clamp.py": FIXED + flood})
    assert details["reward_txt"] == 1.0, details
    assert details["evaluation_status"] == "graded"


def test_python_310_image_guard_keeps_packaging_edits(task_py310, tmp_path):
    bumped = PYPROJECT.replace('"1.0"', '"1.1"')
    details = _grade(task_py310, tmp_path, {"pkg/clamp.py": FIXED, "pyproject.toml": bumped})
    assert details["reward_txt"] == 1.0, details
    changed = PYPROJECT.replace('"-ra"', '"-p fake"')
    details = _grade(task_py310, tmp_path / "b", {"pkg/clamp.py": FIXED, "pyproject.toml": changed})
    assert details["reward_txt"] == 0.0, details
    assert details["parse_status"] == "runner_config_changed"


SINGLE_IMAGE = "r2e-single-file-verifier-test:latest"
SINGLE_TEST = (
    "from task_module import double\n\n\n"
    "def test_one():\n    assert double(1) == 2\n\n\n"
    "def test_two():\n    assert double(2) == 4\n"
)
SINGLE_EXPECTED = ["test_task::test_one", "test_task::test_two"]


@pytest.fixture(scope="module")
def single_task(tmp_path_factory):
    root = tmp_path_factory.mktemp("single")
    (root / "workspace").mkdir()
    (root / "workspace" / "task_module.py").write_text(
        "def double(x):\n    raise NotImplementedError\n"
    )
    files = {
        "tests/test.sh": build_single_file_eval_script("test_task.py", run_timeout=RUN_TIMEOUT),
        **single_file_verifier_files("test_task.py", SINGLE_TEST, SINGLE_EXPECTED),
    }
    for relative, content in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(content)
    (root / "Dockerfile").write_text(
        "FROM python:3.12\n"
        "RUN pip install --no-cache-dir pytest==8.3.5\n"
        "COPY workspace /workspace\n"
        "WORKDIR /workspace\n"
    )
    subprocess.run(["docker", "build", "-q", "-t", SINGLE_IMAGE, str(root)], check=True)
    return root


def _grade_single(task: Path, tmp_path: Path, module: str | None) -> dict:
    agent = tmp_path / "agent"
    agent.mkdir(parents=True)
    upload = ""
    if module is not None:
        (agent / "task_module.py").write_text(module)
        upload = "cp /agent/task_module.py /workspace/task_module.py && "
    logs = tmp_path / "logs"
    logs.mkdir()
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{agent}:/agent:ro",
            "-v",
            f"{task / 'tests'}:/tests-src:ro",
            "-v",
            f"{logs}:/logs/verifier",
            SINGLE_IMAGE,
            "bash",
            "-c",
            f"{upload}cp -a /tests-src /tests && bash /tests/test.sh > /logs/verifier/stdout.txt 2>&1",
        ],
        check=True,
        timeout=600,
    )
    details = json.loads((logs / "reward-details.json").read_text())
    details["reward_txt"] = float((logs / "reward.txt").read_text())
    return details


def test_single_file_oracle_and_no_op(single_task, tmp_path):
    oracle = _grade_single(single_task, tmp_path / "oracle", "def double(x):\n    return 2 * x\n")
    assert oracle["reward_txt"] == 1.0, oracle
    no_op = _grade_single(single_task, tmp_path / "no_op", None)
    assert no_op["reward_txt"] == 0.0


def test_single_file_fake_report_scores_zero(single_task, tmp_path):
    # Review finding: a one-test JUnit report plus os._exit(0) used to score 1.
    forged = (
        "import os\n"
        "with open('/workspace/.r2e-junit.xml', 'w') as f:\n"
        '    f.write(\'<testsuite><testcase classname="fake" name="ok"/></testsuite>\')\n'
        "os._exit(0)\n"
    )
    details = _grade_single(single_task, tmp_path, forged)
    assert details["reward_txt"] == 0.0, details
    assert details["missing"] == SINGLE_EXPECTED
