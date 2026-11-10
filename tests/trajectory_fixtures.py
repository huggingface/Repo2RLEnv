"""Small synthetic projects shared by trajectory contract and live tests."""

from __future__ import annotations

from repo2rlenv.pipelines.trajectory.families import build, extend, qa, repair
from repo2rlenv.pipelines.trajectory.trajectory import ShellRun, Trajectory

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
FORGED = (
    BUGGY
    + "import sys\nif '_pytest.python' in sys.modules:\n    sys.modules['_pytest.python'].Function.runtest = lambda self: None\n"
)
TESTS = "import sys\nsys.path.insert(0, '/app')\n" + "\n".join(
    f"def test_add_{i}():\n    from calc import add\n    assert add({i}, {i + 1}) == {2 * i + 1}\n"
    for i in range(8)
)
NAMES = {f"test_add_{i}" for i in range(8)}


def fixture(name: str):
    trajectory = Trajectory(
        {
            "session_id": "synthetic-review",
            "steps": [
                {"step_id": 1, "source": "user", "message": "Create add"},
                {"step_id": 2, "source": "user", "message": "Make add return the sum"},
            ],
        }
    )
    before = {"calc.py": BUGGY, "other.py": "value = 1\n"}
    after = {**before, "calc.py": FIXED}
    if name == "build":
        return build.Build(), build.BuildCandidate(
            trajectory,
            "/app",
            "Create add",
            after,
            build.GeneratedTests(
                contract="Implement calc.add(a,b), returning the sum.",
                stub_files=[
                    build.GeneratedFile(
                        path="calc.py", content="def add(a,b): raise NotImplementedError\n"
                    )
                ],
                tests=TESTS,
            ),
            NAMES,
            1.0,
        )
    if name == "extend":
        return extend.Extend(), extend.ExtendCandidate(
            trajectory,
            1,
            "/app",
            before,
            after,
            [],
            extend.GeneratedTests(contract="calc.add returns the sum.", tests=TESTS),
            NAMES,
            set(),
            1.0,
        )
    if name == "repair":
        before["test_calc.py"] = after["test_calc.py"] = TESTS
        command = "python -m pytest -v -p no:cacheprovider"
        return repair.Repair(), repair.RepairCandidate(
            trajectory,
            ShellRun(
                3,
                command,
                "/app",
                1,
                "FAILED test_calc.py::test_add_0 - assert -1 == 1",
                before,
                set(),
            ),
            ShellRun(5, command, "/app", 0, "8 passed", after, set()),
            "/app",
            before,
            after,
            ["calc.py"],
            [("pip install pytest==8.4.2", "/app")],
            {"test_calc.py": TESTS},
            sorted("test_calc.py::" + n for n in NAMES),
            [],
        )
    if name == "qa":
        return qa.QA(), qa.QACandidate(
            trajectory,
            qa.ProposedQuestion(
                question="How many data rows are in data.csv?",
                gold_answer="2",
                answer_kind="number",
                evidence_step_id=1,
                evidence_call_id="call1",
                evidence_quote="2",
                inputs=[],
                reference_solution="import os\nfrom pathlib import Path\nprint(len((Path(os.environ['INPUT_DIR']) / 'data.csv').read_text().splitlines()) - 1)",
            ),
            [
                {
                    "repo_id": "synthetic/data",
                    "path_in_repo": "data.csv",
                    "commit": "synthetic",
                    "local_name": "data.csv",
                }
            ],
        )
    raise ValueError(name)


def environment_files(family, candidate):
    if family.name == "qa":
        # A tiny public-data analogue avoids credentials/network in integration tests.
        return {
            "Dockerfile": "FROM python:3.12-bookworm\nRUN apt-get update && apt-get install -y tmux\nCOPY data.csv /home/user/input/data.csv\nWORKDIR /workdir\n",
            "data.csv": "value\n1\n2\n",
        }
    return family.environment_files(candidate)
