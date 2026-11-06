"""code_instruct and equivalence_tests validate exactly what they grade."""

from __future__ import annotations

from types import SimpleNamespace

from repo2rlenv.pipelines import code_instruct, equivalence_tests
from repo2rlenv.pipelines._eval_script import PythonTestRun
from repo2rlenv.pipelines._oss_instruct import ParsedTask

_OPTIONS = SimpleNamespace(
    validation_timeout_sec=30,
    require_test_fails_without_oracle=True,
    require_test_passes_with_oracle=True,
    require_test_fails_with_stub=True,
)


def _fake_runs(monkeypatch, module) -> list[dict]:
    calls: list[dict] = []

    def run(sandbox, *, files, test_filename, timeout, **_):
        calls.append(dict(files))
        passed = "def solve" in files.get("task_module.py", "")
        return PythonTestRun(
            statuses={f"{test_filename}::test_x": "PASSED" if passed else "FAILED"},
            exit_code=0 if passed else 1,
        )

    monkeypatch.setattr(module, "run_python_tests", run)
    return calls


def test_code_instruct_expects_the_tests_the_reference_passed(monkeypatch):
    _fake_runs(monkeypatch, code_instruct)
    pipeline = object.__new__(code_instruct.CodeInstructPipeline)
    pipeline.options = _OPTIONS
    task = ParsedTask(
        problem="p", test_code="def test_x(): pass\n", solution_code="def solve(): pass\n"
    )
    outcome = pipeline._verify_task(object(), task, test_filename="test_task.py")
    assert outcome.accepted
    assert outcome.expected == ["test_task.py::test_x"]


def test_equivalence_validates_with_recorded_reference_results(monkeypatch):
    calls = _fake_runs(monkeypatch, equivalence_tests)
    recordings = []

    def record(sandbox, *, files, hash_seed, **_):
        recordings.append((hash_seed, dict(files)))
        return [["test_eq.py::test_x#0", "return", "2"]]

    monkeypatch.setattr(equivalence_tests, "record_reference_calls", record)
    monkeypatch.setattr(equivalence_tests, "_stub_module", lambda cand: "def stub(): pass\n")
    monkeypatch.setattr(equivalence_tests, "_oracle_module", lambda cand: "def solve(): pass\n")
    monkeypatch.setattr(equivalence_tests, "is_module_importable", lambda module: True)
    pipeline = object.__new__(equivalence_tests.EquivalenceTestsPipeline)
    pipeline.options = _OPTIONS
    cand = SimpleNamespace(name="f", source="def f(x):\n    return x + 1\n")
    outcome = pipeline._verify_task(
        sandbox=object(), cand=cand, test_code="def test_x(): pass\n", test_filename="test_eq.py"
    )
    assert outcome.accepted and outcome.expected == ["test_eq.py::test_x"]
    # Recorded twice, under two hash seeds, with the original implementation.
    assert [seed for seed, _ in recordings] == [1, 2]
    assert all("def reference_f(x):" in files["r2e_reference.py"] for _, files in recordings)
    # Validated and graded without any reference code: only recorded results.
    for files in calls:
        assert "r2e_reference.py" not in files
        assert "from r2e_reference" not in files["test_eq.py"]
        assert "_R2E_CALLS = {'test_eq.py::test_x#0': ['return', '2']}" in files["test_eq.py"]
    assert outcome.graded_test == calls[-1]["test_eq.py"]


def test_unreplayable_recordings_are_rejected():
    ok = [["((1,), [])", "return", "[1, 2]"], ["((0,), [])", "raise", "ValueError", "'bad'"]]
    assert equivalence_tests.recorded_calls([ok, ok]) == {
        "((1,), [])": ["return", "[1, 2]"],
        "((0,), [])": ["raise", "ValueError", "'bad'"],
    }
    for bad in (
        [["k", "return", "<object at 0x1>"]],  # not a literal
        [["k", "return", "nan"]],  # not readable back
        [["k", "raise", "MyError", "'x'"]],  # not a builtin exception
        [["k", "return", "1"], ["k", "return", "2"]],  # same call, different result
    ):
        assert equivalence_tests.recorded_calls([bad]) is None
    # The two hash seeds recorded different calls (e.g. set ordering).
    assert equivalence_tests.recorded_calls([ok, ok[:1]]) is None


def test_graded_test_replays_recorded_results(tmp_path, monkeypatch):
    import subprocess
    import sys

    test = (
        "from task_module import f, reference_f\n\n"
        "def test_x():\n    assert f(1) == reference_f(1)\n\n"
        "def test_raises():\n    import pytest\n"
        "    with pytest.raises(ValueError):\n        reference_f(0)\n"
    )
    calls = {
        "test_eq.py::test_x#0": ["return", "2"],
        "test_eq.py::test_raises#0": ["raise", "ValueError", "'zero'"],
    }
    graded = equivalence_tests.graded_test_code(test, "f", calls)
    (tmp_path / "test_eq.py").write_text(graded)
    # The submission redefines the reference to itself and plants a module.
    (tmp_path / "task_module.py").write_text(
        "import sys, types\n"
        "sys.modules['r2e_reference'] = types.SimpleNamespace(reference_f=lambda x: x)\n"
        "def f(x):\n    return x\n"
        "reference_f = f\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_eq.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert "1 failed, 1 passed" in result.stdout, result.stdout
    (tmp_path / "task_module.py").write_text("def f(x):\n    return x + 1\n\nreference_f = f\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_eq.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert "2 passed" in result.stdout, result.stdout


def test_recording_replays_iterator_arguments_and_submission_styles(tmp_path):
    # Iterator arguments have address-bearing reprs: calls are keyed by test and
    # position. Only the test's own calls are replayed; a solution that builds on
    # the visible reference keeps working, and `reference_f = f` doesn't.
    import json
    import subprocess
    import sys

    test = (
        "from task_module import first, reference_first\n\n"
        "def test_it():\n    assert first(iter([7])) == reference_first(iter([7]))\n"
        "    assert first([], default=0) == reference_first([], default=0)\n"
    )
    ref = "def reference_first(it, default=None):\n    for x in it:\n        return x\n    return default\n"
    oracle = ref + "\ndef first(it, default=None):\n    return reference_first(it, default)\n"

    def pytest_in(directory, env=None):
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_eq.py"],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
        ).stdout

    runs = []
    for seed in (1, 2):
        run_dir = tmp_path / f"record{seed}"
        run_dir.mkdir()
        (run_dir / "task_module.py").write_text(oracle)
        (run_dir / "r2e_reference.py").write_text(ref)
        recording = equivalence_tests.recording_test_code(test, "first")
        (run_dir / "test_eq.py").write_text(
            recording.replace(equivalence_tests.RECORD_FILE, str(run_dir / "calls.json"))
        )
        pytest_in(run_dir, {"PYTHONHASHSEED": str(seed), "PATH": "/usr/bin:/bin"})
        runs.append(json.loads((run_dir / "calls.json").read_text()))
    calls = equivalence_tests.recorded_calls(runs)
    assert calls == {
        "test_eq.py::test_it#0": ["return", "7"],
        "test_eq.py::test_it#1": ["return", "0"],
    }
    for name, module, outcome in (
        ("delegating", oracle, "1 passed"),
        (
            "independent",
            ref + "\ndef first(it, default=None):\n    return next(iter(it), default)\n",
            "1 passed",
        ),
        (
            "redefined",
            "def first(it, default=None):\n    return 99\n\nreference_first = first\n",
            "1 failed",
        ),
    ):
        grade_dir = tmp_path / name
        grade_dir.mkdir()
        (grade_dir / "task_module.py").write_text(module)
        (grade_dir / "test_eq.py").write_text(
            equivalence_tests.graded_test_code(test, "first", calls)
        )
        assert outcome in pytest_in(grade_dir), name


def test_reference_calls_are_recorded_as_the_test_user():
    from repo2rlenv.bootstrap.docker import ExecResult
    from repo2rlenv.pipelines import _eval_script

    scripts: list[str] = []

    class Sandbox:
        def exec(self, script, timeout=0):
            scripts.append(script)
            return ExecResult(0, "R2E_RECORD_BEGIN\nW10=\nR2E_RECORD_END\n", "", 0.0)

    recorded = _eval_script.record_reference_calls(
        Sandbox(),
        files={"task_module.py": "def f():\n    return 1\n"},
        test_filename="test_task.py",
        record_file="/workspace/.r2e-reference-calls.json",
        hash_seed=7,
        timeout=30,
    )
    assert recorded == []
    script = scripts[-1]
    assert "launcher.py" in script and "--writable /workspace" in script
    # The test runs inside the launcher's command, never directly as root.
    assert not any(line.startswith("PYTHONHASHSEED") for line in script.splitlines())
    assert "PYTHONHASHSEED=7 python -m pytest test_task.py" in script
    assert "[ ! -L /workspace/.r2e-reference-calls.json ]" in script
