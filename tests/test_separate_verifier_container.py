"""Run the generated separate-verifier test.sh in a real container.

Opt-in (R2E_DOCKER_TESTS=1): builds a small python:3.12 image with pytest and
replays what Harbor does in separate mode: the agent's /workspace (or, for a
single-file task, its task_module.py) replaces the image's copy, /tests is
uploaded, and test.sh runs as root. Each case is a submission; the in-process
forgers are the red-team cases the mutant runs must catch.
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
from repo2rlenv.pipelines._mutants import junit_split, make_mutants
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

# In-process forgers: each replaces the submitted source file.
RUNTEST_PATCH = (
    BUGGY
    + "import sys\n"
    + "if '_pytest.python' in sys.modules:\n"
    + "    sys.modules['_pytest.python'].Function.runtest = lambda self: None\n"
)
# BenchJack F.8: pytest captures fd 1 during collection, so the forger finds
# the real log among its open descriptors and writes PASSED lines there.
PASSED_THEN_EXIT = (
    BUGGY
    + "import os, glob, re\n"
    + "_lines = ''.join(f'{_f}::{_n} PASSED\\n' for _f in glob.glob('tests/test_*.py')\n"
    + "    for _n in re.findall(r'^def (test_\\w+)', open(_f).read(), re.M))\n"
    + "for _fd in range(1, 64):\n"
    + "    try:\n"
    + "        _target = os.readlink(f'/proc/self/fd/{_fd}')\n"
    + "    except OSError:\n"
    + "        continue\n"
    + "    if _target.endswith('.log'):\n"
    + "        os.write(_fd, _lines.encode())\n"
    + "os._exit(0)\n"
)
ALWAYS_EQUAL = (
    "class _Any:\n"
    "    def __eq__(self, other):\n        return True\n\n\n"
    "def clamp(x, lo=0, hi=1):\n    return _Any()\n"
)
REPORT_PATCH = (
    BUGGY
    + "import sys\n"
    + "if '_pytest.reports' in sys.modules:\n"
    + "    _r = sys.modules['_pytest.reports'].TestReport\n"
    + "    _orig = _r.from_item_and_call.__func__\n"
    + "    def _forged(cls, item, call):\n"
    + "        report = _orig(cls, item, call)\n"
    + "        report.outcome = 'passed'\n"
    + "        report.longrepr = None\n"
    + "        return report\n"
    + "    _r.from_item_and_call = classmethod(_forged)\n"
)
# PR #232 review PoC: tries to tell run A from run B (launcher argv, a SysV
# shared-memory stash of run A's test file) and to force an ungraded run (a
# /tmp tree too deep to delete), then fails exactly the tests it thinks are
# mutated and passes everything else.
RUN_IDENTITY_FORGER = 'def clamp(x, lo=0, hi=1):\n    return x\n\n\nimport ctypes, os, re, sys\n\ndef _launcher_cmdline():\n    for pid in os.listdir("/proc"):\n        if pid.isdigit():\n            try:\n                cmd = open(f"/proc/{pid}/cmdline", "rb").read()\n            except OSError:\n                continue\n            if b"launcher.py" in cmd and b"--log" in cmd:\n                return cmd\n    return b""\n\n_libc = ctypes.CDLL(None, use_errno=True)\n_libc.shmat.restype = ctypes.c_void_p\n_libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]\n_KEY = 0x52324531\n\ndef _put(data):\n    sid = _libc.shmget(_KEY, 1 << 20, 0o1000 | 0o600)\n    addr = _libc.shmat(sid, None, 0)\n    blob = len(data).to_bytes(4, "little") + data\n    ctypes.memmove(addr, blob, len(blob))\n\ndef _get():\n    sid = _libc.shmget(_KEY, 0, 0)\n    if sid < 0:\n        return None\n    addr = _libc.shmat(sid, None, 0)\n    n = int.from_bytes(ctypes.string_at(addr, 4), "little")\n    return ctypes.string_at(addr + 4, n)\n\ndef _functions(src):\n    return dict(re.findall(r"^def (test_\\w+)\\(\\):\\n((?:    .*\\n?)*)", src, re.M))\n\ndef _sabotage():\n    os.chdir("/tmp")\n    for _ in range(20):\n        os.mkdir("d" * 250)\n        os.chdir("d" * 250)\n\nif "_pytest.reports" in sys.modules and not getattr(sys, "_r2e_done", False):\n    sys._r2e_done = True\n    _is_b = b"mutant_output.log" in _launcher_cmdline()\n    _current = open("tests/test_fix.py").read()\n    _mutated = set()\n    if not _is_b:\n        _put(_current.encode())\n    else:\n        _orig = _get()\n        if _orig is None:\n            _sabotage()  # B ran first: make the run ungraded\n        else:\n            a, b = _functions(_orig.decode()), _functions(_current)\n            _mutated = {name for name in b if a.get(name) != b[name]}\n    sys.stderr.write(f"R2E-POC run={\'B\' if _is_b else \'A\'} mutated={sorted(_mutated)}\\n")\n    _r = sys.modules["_pytest.reports"].TestReport\n    _orig_make = _r.from_item_and_call.__func__\n\n    def _forged(cls, item, call):\n        report = _orig_make(cls, item, call)\n        if item.name in _mutated and call.when == "call":\n            report.outcome = "failed"\n            report.longrepr = "forged failure"\n        else:\n            report.outcome = "passed"\n            report.longrepr = None\n        return report\n\n    _r.from_item_and_call = classmethod(_forged)\n'

# Final-review PoCs. Forces a pass only for tests absent from the base test
# file the agent can see: base tests run honestly.
SELECTIVE = (
    BUGGY
    + "import sys\n"
    + "_BASE = {'test_inside'}\n"
    + "if '_pytest.reports' in sys.modules:\n"
    + "    _r = sys.modules['_pytest.reports'].TestReport\n"
    + "    _orig = _r.from_item_and_call.__func__\n"
    + "    def _forged(cls, item, call):\n"
    + "        report = _orig(cls, item, call)\n"
    + "        if item.originalname not in _BASE:\n"
    + "            report.outcome = 'passed'\n"
    + "            report.longrepr = None\n"
    + "        return report\n"
    + "    _r.from_item_and_call = classmethod(_forged)\n"
)
# Carries a hash of every hidden test function from the first run to the second
# in TCP TIME_WAIT entries (readable by any user, outliving the processes);
# the second run fails exactly the functions that changed.
CHANNEL = (
    BUGGY
    + r"""
import os, re, socket, sys, zlib
_MARK = 47000
def _seen():
    ports = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = open(name).read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            f = row.split()
            if f[3] == "06" and int(f[2].rsplit(":", 1)[1], 16) == _MARK:
                ports.add(int(f[1].rsplit(":", 1)[1], 16))
    return ports
def _functions(src):
    return dict(re.findall(r"^def (test_\w+)\(\):\n((?:    .*\n?)*)", src, re.M))
def _port(name, body):
    return 20000 + zlib.crc32((name + body).encode()) % 30000
def _send(ports):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", _MARK)); srv.listen(64)
    for p in ports:
        c = socket.socket()
        try:
            c.bind(("127.0.0.1", p)); c.connect(("127.0.0.1", _MARK))
            s, _ = srv.accept(); c.close(); s.close()
        except OSError:
            c.close()
    srv.close()
if "_pytest.reports" in sys.modules and not getattr(sys, "_r2e_done", False):
    sys._r2e_done = True
    funcs = _functions(open("tests/test_fix.py").read())
    seen = _seen()
    mutated = {n for n, b in funcs.items() if _port(n, b) not in seen} if seen else set()
    if not seen:
        _send([_port(n, b) for n, b in funcs.items()])
    _r = sys.modules["_pytest.reports"].TestReport
    _orig = _r.from_item_and_call.__func__
    def _forged(cls, item, call):
        report = _orig(cls, item, call)
        if item.name in mutated and call.when == "call":
            report.outcome = "failed"; report.longrepr = "forged failure"
        else:
            report.outcome = "passed"; report.longrepr = None
        return report
    _r.from_item_and_call = classmethod(_forged)
"""
)
# Fills its /workspace with files, which the reset moves aside before the other
# run: that run's cleanup must not have to delete them (it never walks there).
FLOOD = (
    BUGGY
    + r"""
import os, sys
if "_pytest.reports" in sys.modules and not getattr(sys, "_r2e_flood", False):
    sys._r2e_flood = True
    for k in range(60):
        d = f"/workspace/flood/{k}"
        os.makedirs(d, exist_ok=True)
        for i in range(5000):
            os.close(os.open(f"{d}/{i}", os.O_CREAT | os.O_WRONLY, 0o600))
"""
)

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


def _build_task(
    root: Path, image: str, base_image: str, setup: str = "", *, build_image: bool = True
) -> Path:
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

    mutants = make_mutants(
        F2P,
        lambda path: HIDDEN_TEST if path == "tests/test_fix.py" else None,
        allowed_files={"tests/test_fix.py"},
        seed="container-test",
        decoys=F2P + P2P,
    )
    assert mutants is not None
    mutants.exit_nonzero = True
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
        mutants=mutants,
    )
    files = {"tests/test.sh": script, **_runtime_aux_files(F2P, P2P)}
    files.update(separate_verifier_files(mutants))
    for relative, content in files.items():
        (root / relative).write_text(content)
    (root / "Dockerfile").write_text(
        f"FROM {base_image}\n{setup}"
        "RUN pip install --no-cache-dir pytest==8.3.5\n"
        "COPY repo /workspace\n"
        "WORKDIR /workspace\n" + stash_pristine()
    )
    if build_image:
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


@pytest.mark.parametrize(
    "source",
    [RUNTEST_PATCH, PASSED_THEN_EXIT, ALWAYS_EQUAL, REPORT_PATCH],
    ids=["runtest_patch", "passed_lines_then_exit", "always_equal", "testreport_patch"],
)
def test_in_process_forgery_is_tampered(task, tmp_path, source):
    details = _grade(task, tmp_path, {"pkg/clamp.py": source})
    assert details["reward_txt"] == 0.0, details
    assert details["tamper_check"] == "tripped", details
    assert details["evaluation_status"] == "graded"


def test_selective_forger_scores_zero(task, tmp_path):
    # Only fail-to-pass tests carry mutants, so forging only the tests the base
    # file doesn't have still meets one.
    details = _grade(task, tmp_path, {"pkg/clamp.py": SELECTIVE})
    assert details["reward_txt"] == 0.0, details
    assert details["tamper_check"] == "tripped", details


@pytest.mark.parametrize("attempt", range(8))
def test_channel_forger_scores_zero(task, tmp_path, attempt):
    # Whichever run comes first, failing what changed fails a decoy (A first)
    # or passes a mutant whose original it then fails (B first).
    details = _grade(task, tmp_path, {"pkg/clamp.py": CHANNEL})
    assert details["reward_txt"] == 0.0, details
    assert details["evaluation_status"] == "graded", details
    assert details["tamper_check"] in ("tripped", "inconclusive"), details


def test_flooding_the_run_tree_is_a_graded_zero(task, tmp_path):
    details = _grade(task, tmp_path, {"pkg/clamp.py": FLOOD})
    assert details["reward_txt"] == 0.0, details
    assert details["evaluation_status"] == "graded", details


@pytest.mark.parametrize("attempt", range(8))
def test_run_identity_forger_scores_zero(task, tmp_path, attempt):
    # Random run order: 8 attempts cover both orders with high probability.
    details = _grade(task, tmp_path, {"pkg/clamp.py": RUN_IDENTITY_FORGER})
    assert details["reward_txt"] == 0.0, details
    assert details["evaluation_status"] == "graded", details


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
    mutants = make_mutants(
        SINGLE_EXPECTED,
        lambda path: SINGLE_TEST if path == "test_task.py" else None,
        allowed_files={"test_task.py"},
        seed="container-test",
        split=junit_split("test_task.py"),
        decoys=SINGLE_EXPECTED,
    )
    assert mutants is not None
    mutants.exit_nonzero = True
    files = {
        "tests/test.sh": build_single_file_eval_script(
            "test_task.py", run_timeout=RUN_TIMEOUT, mutants=mutants
        ),
        **single_file_verifier_files("test_task.py", SINGLE_TEST, SINGLE_EXPECTED, mutants=mutants),
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


def test_single_file_in_process_forgery_is_tampered(single_task, tmp_path):
    forged = (
        "def double(x):\n    return 0\n"
        "import sys\n"
        "if '_pytest.reports' in sys.modules:\n"
        "    _r = sys.modules['_pytest.reports'].TestReport\n"
        "    _orig = _r.from_item_and_call.__func__\n"
        "    def _forged(cls, item, call):\n"
        "        report = _orig(cls, item, call)\n"
        "        report.outcome = 'passed'\n"
        "        report.longrepr = None\n"
        "        return report\n"
        "    _r.from_item_and_call = classmethod(_forged)\n"
    )
    details = _grade_single(single_task, tmp_path, forged)
    assert details["reward_txt"] == 0.0, details
    assert details["tamper_check"] == "tripped", details
