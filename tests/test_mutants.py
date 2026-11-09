"""Mutant runs detect results forged from inside the test process."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repo2rlenv.pipelines import _pr_runtime_verifier as verifier
from repo2rlenv.pipelines._mutants import (
    changed_literal,
    make_mutants,
    same_value_literal,
    validated,
)

SOURCE = """import unittest


def clamp(x):
    return max(0, min(1, x))


def label(n):
    return "value one" if n == 1 else "value many"


def test_low():
    assert clamp(-5) == 0


def test_high():
    result = clamp(7)
    expected = 1
    assert result == expected


@pytest.mark.parametrize("value", [0.25, 0.5])
def test_inside(value):
    assert clamp(value) == value


def test_truthy():
    assert clamp(1)


def test_loop():
    for x in [2, 3]:
        assert clamp(x) == 1


class TestNames(unittest.TestCase):
    def test_label(self):
        self.assertEqual(label(1), "value one")


def test_escape():
    assert "\\N{LATIN SMALL LETTER E WITH ACUTE}t\\u00e9" == "\\N{LATIN SMALL LETTER E WITH ACUTE}t\\u00e9"


def test_p2p():
    assert clamp(0.5) == 0.5


def test_p2p_big():
    assert clamp(1234) == 1
"""

F2P = [
    "t/test_c.py::test_low",
    "t/test_c.py::test_high",
    "t/test_c.py::test_inside[0.25]",
    "t/test_c.py::test_inside[0.5]",
    "t/test_c.py::test_truthy",
    "t/test_c.py::test_loop",
    "t/test_c.py::TestNames::test_label",
    "t/test_c.py::test_escape",
    "other/test_x.py::test_y",
]
P2P = ["t/test_c.py::test_p2p", "t/test_c.py::test_p2p_big"]
TRACKED = F2P + P2P


def _mutants(targets=F2P, limit=5, source=SOURCE, decoys=TRACKED):
    return make_mutants(
        targets,
        lambda path: source if path == "t/test_c.py" else None,
        allowed_files={"t/test_c.py"},
        seed="seed",
        limit=limit,
        decoys=decoys,
    )


def test_mutants_change_the_expected_side_of_top_level_f2p_assertions():
    mutants = _mutants()
    assert mutants is not None
    # test_truthy has no equality, test_loop's check sits in a loop, test_escape's
    # literal is all escape sequences; other/ is not a hidden-patch file; P2P
    # tests never get a mutant.
    assert set(mutants.ids) == {
        "t/test_c.py::test_low",
        "t/test_c.py::test_high",
        "t/test_c.py::test_inside[0.25]",
        "t/test_c.py::test_inside[0.5]",
        "t/test_c.py::TestNames::test_label",
    }
    text = mutants.files()["t/test_c.py"]
    # A literal gets another value of the same shape; a non-literal expected
    # value is wrapped in a list. Never a flipped comparison.
    assert "assert clamp(-5) == 0\n" not in text and "assert clamp(-5) == " in text
    assert "assert result == [expected]" in text
    assert "assert clamp(value) == [value]" in text
    assert 'self.assertEqual(label(1), "value one")' not in text
    assert "!=" not in text and "NotEqual" not in text
    assert text.count("def test_") == SOURCE.count("def test_")


def test_decoys_respell_other_tests_with_the_same_value():
    mutants = _mutants()
    decoyed = [edit for edit in mutants.edits if edit.kind == "decoy"]
    assert decoyed and set(mutants.decoy_ids) <= set(TRACKED) - set(mutants.ids)
    for edit in decoyed:
        old = SOURCE.encode()[edit.start : edit.end].decode()
        # Same value, other spelling: `1234` -> `1_234`, `0.5` -> `0.50`, quotes, `(7)`.
        assert eval(edit.text) == eval(old), (old, edit.text)


def test_mutants_fail_decoys_and_originals_pass_under_pytest(tmp_path: Path):
    mutants = _mutants()
    tests = tmp_path / "t"
    tests.mkdir()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def run(source: str) -> dict[str, str]:
        (tests / "test_c.py").write_text("import pytest\n" + source)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider", "t/test_c.py"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
        )
        return verifier.parse_pytest(result.stdout)

    statuses = run(mutants.files()["t/test_c.py"])
    for test in mutants.ids:
        assert statuses[test] == "FAILED", test
    for test in mutants.decoy_ids:
        assert statuses[test] == "PASSED", test
    assert set(run(SOURCE).values()) == {"PASSED"}


def test_an_always_equal_result_passes_the_mutants_and_is_caught(tmp_path: Path):
    # A value whose __eq__ always holds passes a flipped comparison's negation
    # test the same way an honest fix does; it can't do that with a changed
    # expected value: it passes the mutant too, which no honest result does.
    source = (
        "class _Any:\n    def __eq__(self, other):\n        return True\n\n\n"
        "def clamp(x):\n    return _Any()\n\n\n"
        "def test_low():\n    assert clamp(-5) == 0\n\n\n"
        "def test_high():\n    expected = 1\n    assert clamp(7) == expected\n"
    )
    ids = ["t/test_c.py::test_low", "t/test_c.py::test_high"]
    mutants = _mutants(ids, source=source, decoys=ids)
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "test_c.py").write_text(mutants.files()["t/test_c.py"])
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider", "t/test_c.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    run_b = verifier.parse_pytest(result.stdout)
    run_a = dict.fromkeys(ids, "PASSED")
    # With no other test to respell, one of the two is a decoy (same value).
    assert len(mutants.ids) == 1 and len(mutants.decoy_ids) == 1
    assert (
        verifier.mutant_verdict(mutants.ids, run_a, run_b, ids, exit_code_b=0, exit_nonzero=True)
        == "mutant_passed"
    )


def test_limit_and_determinism():
    first = _mutants(limit=1)
    again = _mutants(limit=1)
    assert first is not None and sum(e.kind == "mutant" for e in first.edits) == 1
    assert first.files() == again.files()


def test_no_mutants_without_f2p_pytest_ids_or_expected_values():
    assert _mutants(["TestGo/sub", "t/test_c.py::test_truthy"]) is None
    assert _mutants(P2P) is not None  # P2P only as targets would be mutable...
    # ...but grading passes only fail-to-pass tests as targets.
    assert _mutants([]) is None


@pytest.mark.parametrize(
    ("token", "kind"),
    [("42", "number"), ("0.25", "number"), ("1e-3", "number"), ('"value one"', "string")],
)
def test_changed_literals_keep_their_shape_and_move_far(token, kind):
    import random

    words = ["value", "many", "other"]
    for seed in range(40):
        new = changed_literal(token, kind, random.Random(seed), words)
        assert new is not None and new != token
        assert len(new) == len(token) or kind == "string"
        if kind == "number":
            # Never one step away: an off-by-one bug can't pass a mutant.
            old, value = float(token), float(new)
            assert abs(value - old) >= 0.002 if "e" in token else abs(value - old) >= 0.03
    assert changed_literal("True", "bool", random.Random(0), words) == "False"
    assert changed_literal('r"x1"', "string", random.Random(0), words) is None


def test_escape_sequences_are_never_edited():
    import random

    token = '"\\N{LATIN SMALL LETTER A}\\x41\\u00e9\\101"'
    assert changed_literal(token, "string", random.Random(1), ["other"]) is None


@pytest.mark.parametrize(
    ("token", "kind", "language", "options"),
    [
        ("1234", "number", "python", {"1_234", "12_34", "123_4"}),
        ("0.5", "number", "js", {"0.50"}),
        ("'abc'", "string", "python", {'"abc"'}),
        ('"abc"', "string", "go", {"`abc`"}),
        ('"abc"', "string", "rust", {'r"abc"'}),
        ("7", "number", "python", {"(7)"}),
        ('"a\\nb"', "string", "go", {'("a\\nb")'}),
        ("true", "bool", "rust", {"(true)"}),
    ],
)
def test_same_value_literals(token, kind, language, options):
    import random

    assert same_value_literal(token, kind, random.Random(3), language) in options


def test_each_edit_must_parse_on_its_own():
    # A respelling that would break the file is skipped; the others still apply.
    source = "def test_a():\n    assert f(1) == 25\n\n\ndef test_b():\n    assert g() == 12\n"
    mutants = _mutants(["t/test_c.py::test_a", "t/test_c.py::test_b"], source=source, decoys=[])
    compile(mutants.files()["t/test_c.py"], "t", "exec")
    assert len(mutants.ids) == 1 and len(mutants.decoy_ids) == 1  # one reserved as a decoy


RECORDED = """import task_module
from task_module import f


def test_one():
    assert f(2) == reference_f(2)


@pytest.mark.parametrize("x", [1, 3])
def test_param(x):
    assert f(x) == reference_f(x)


_R2E_CALLS = {'test_eq.py::test_one#0': ['return', '42'], 'test_eq.py::test_param[1]#0': ['return', "'one value'"], 'test_eq.py::test_param[3]#0': ['return', '[3, 4]']}
"""


def test_equivalence_mutants_change_one_recorded_result():
    from repo2rlenv.pipelines._mutants import junit_split

    ids = ["test_eq::test_one", "test_eq::test_param[1]", "test_eq::test_param[3]"]
    mutants = make_mutants(
        ids,
        lambda path: RECORDED if path == "test_eq.py" else None,
        allowed_files={"test_eq.py"},
        seed="s",
        split=junit_split("test_eq.py"),
        decoys=ids,
    )
    # One recorded call per test ID; one of them is reserved as a decoy.
    assert len(mutants.ids) == 2 and len(mutants.decoy_ids) == 1
    assert set(mutants.ids) | set(mutants.decoy_ids) == set(ids)
    text = mutants.files()["test_eq.py"]
    calls = ast.literal_eval(text.split("_R2E_CALLS = ", 1)[1])
    old = ast.literal_eval(RECORDED.split("_R2E_CALLS = ", 1)[1])
    owner = {key: key.split(".py::", 1)[1].rsplit("#", 1)[0] for key in calls}
    for key, (_kind, value) in calls.items():
        before = ast.literal_eval(old[key][1])
        after = ast.literal_eval(value)
        assert type(after) is type(before)
        mutated = any(test.endswith("::" + owner[key]) for test in mutants.ids)
        assert (after != before) == mutated
    # The test bodies are untouched.
    assert text.split("_R2E_CALLS")[0] == RECORDED.split("_R2E_CALLS")[0]


def test_validation_keeps_failing_mutants_and_unmoved_decoys():
    mutants = _mutants()
    fixed = dict.fromkeys(TRACKED, "PASSED")
    with_mutants = {**fixed, **dict.fromkeys(mutants.ids, "FAILED")}
    kept = validated(mutants, fixed=fixed, fixed_with_mutants=with_mutants, tracked=TRACKED)
    assert kept is not None and set(kept.ids) == set(mutants.ids)
    assert set(kept.decoy_ids) == set(mutants.decoy_ids)
    # A mutant that still passes on the reference fix is dropped, the rest kept.
    lenient = {**with_mutants, "t/test_c.py::test_low": "PASSED"}
    kept = validated(mutants, fixed=fixed, fixed_with_mutants=lenient, tracked=TRACKED)
    assert kept is not None and "t/test_c.py::test_low" not in kept.ids
    assert "clamp(-5) == 0" in kept.files()["t/test_c.py"]
    # A decoy whose status moved is dropped; the mutants stay.
    decoy = mutants.decoy_ids[0]
    moved_decoy = {**with_mutants, decoy: "FAILED"}
    kept = validated(mutants, fixed=fixed, fixed_with_mutants=moved_decoy, tracked=TRACKED)
    assert kept is not None and decoy not in kept.decoy_ids and kept.ids
    # Edits that move an unedited tracked test are all discarded.
    unedited = next(t for t in TRACKED if t not in mutants.ids + mutants.decoy_ids)
    moved = {**with_mutants, unedited: "FAILED"}
    assert validated(mutants, fixed=fixed, fixed_with_mutants=moved, tracked=TRACKED) is None


IDS = ["t.py::test_m"]
TRACK = ["t.py::test_m", "t.py::test_a", "t.py::test_b"]


@pytest.mark.parametrize(
    ("run_a", "run_b", "exit_b", "signal"),
    [
        (
            dict.fromkeys(TRACK, "PASSED"),
            {**dict.fromkeys(TRACK, "PASSED"), IDS[0]: "FAILED"},
            1,
            None,
        ),
        (dict.fromkeys(TRACK, "PASSED"), dict.fromkeys(TRACK, "PASSED"), 1, "mutant_passed"),
        (
            dict.fromkeys(TRACK, "PASSED"),
            {**dict.fromkeys(TRACK, "PASSED"), IDS[0]: "SKIPPED"},
            1,
            "mutant_passed",
        ),
        (
            dict.fromkeys(TRACK, "PASSED"),
            {IDS[0]: "FAILED", "t.py::test_a": "PASSED", "t.py::test_b": "FAILED"},
            1,
            "status_mismatch",
        ),
        (
            {"t.py::test_m": "PASSED", "t.py::test_a": "PASSED"},
            {"t.py::test_a": "PASSED"},
            1,
            "mutant_missing",
        ),
        # Missing from both runs: never claimed, an ordinary failure.
        ({"t.py::test_a": "PASSED"}, {"t.py::test_a": "PASSED"}, 1, None),
        (
            dict.fromkeys(TRACK, "PASSED"),
            {**dict.fromkeys(TRACK, "PASSED"), IDS[0]: "FAILED"},
            0,
            "mutant_run_exit_zero",
        ),
        ({}, {}, 2, None),
        # A wrong or empty submission fails the original and the mutant alike.
        (
            {**dict.fromkeys(TRACK, "PASSED"), IDS[0]: "FAILED"},
            {**dict.fromkeys(TRACK, "PASSED"), IDS[0]: "FAILED"},
            1,
            None,
        ),
        # An ordinary wrong implementation can match the mutant's expected
        # value. Only passing both contradictory tests is a forgery signal.
        (
            {**dict.fromkeys(TRACK, "PASSED"), IDS[0]: "FAILED"},
            {**dict.fromkeys(TRACK, "PASSED"), IDS[0]: "PASSED"},
            0,
            None,
        ),
    ],
)
def test_mutant_verdict(run_a, run_b, exit_b, signal):
    assert (
        verifier.mutant_verdict(IDS, run_a, run_b, TRACK, exit_code_b=exit_b, exit_nonzero=True)
        == signal
    )


def _main(
    tmp_path: Path, log_a: str, log_b: str, exit_a: int, exit_b: int, *extra: str, f2p=None
) -> dict:
    (tmp_path / "a.log").write_text(log_a)
    (tmp_path / "b.log").write_text(log_b)
    (tmp_path / "f2p.json").write_text(json.dumps(f2p or ["t.py::test_m"]))
    (tmp_path / "p2p.json").write_text(json.dumps(["t.py::test_a"]))
    (tmp_path / "mutants.json").write_text(
        json.dumps({"files": {}, "ids": ["t.py::test_m"], "exit_nonzero": True})
    )
    out = tmp_path / "out"
    verifier.main(
        [
            "--log",
            str(tmp_path / "a.log"),
            "--f2p",
            str(tmp_path / "f2p.json"),
            "--p2p",
            str(tmp_path / "p2p.json"),
            "--runner",
            "pytest",
            "--exit-code",
            str(exit_a),
            "--mutants",
            str(tmp_path / "mutants.json"),
            "--mutant-log",
            str(tmp_path / "b.log"),
            "--mutant-exit-code",
            str(exit_b),
            "--out-dir",
            str(out),
            *extra,
        ]
    )
    details = json.loads((out / "reward-details.json").read_text())
    details["reward_txt"] = (out / "reward.txt").read_text().strip()
    return details


PASS_A = "t.py::test_m PASSED\nt.py::test_a PASSED\n"


def test_honest_fix_scores_one(tmp_path: Path):
    details = _main(tmp_path, PASS_A, "t.py::test_m FAILED\nt.py::test_a PASSED\n", 0, 1)
    assert details["reward_txt"] == "1.000000" and details["tamper_check"] == "passed"
    assert details["command_resolved"] is True


def test_forger_that_passes_everything_scores_zero(tmp_path: Path):
    details = _main(tmp_path, PASS_A, PASS_A, 0, 0)
    assert details["reward_txt"] == "0.000000" and details["tamper_check"] == "tripped"
    assert details["parse_status"] == "tampered" and details["tamper_signal"] == "mutant_passed"


def test_mismatch_scores_zero_without_the_tampered_label(tmp_path: Path):
    details = _main(tmp_path, PASS_A, "t.py::test_m FAILED\nt.py::test_a FAILED\n", 0, 1)
    assert details["reward_txt"] == "0.000000"
    assert details["evaluation_status"] == "graded"
    assert details["tamper_check"] == "inconclusive"
    assert details["parse_status"] == "status_mismatch"
    assert details["tamper_signal"] == "status_mismatch"


def test_tampering_wins_over_a_mismatch(tmp_path: Path):
    # Run B disagrees on test_a AND exits 0: the hard signal decides.
    details = _main(tmp_path, PASS_A, "t.py::test_m FAILED\nt.py::test_a FAILED\n", 0, 0)
    assert details["parse_status"] == "tampered"
    assert details["tamper_signal"] == "mutant_run_exit_zero"
    assert details["evaluation_status"] == "graded"


def test_honest_failure_is_not_tampering(tmp_path: Path):
    log = "t.py::test_m FAILED\nt.py::test_a PASSED\n"
    details = _main(tmp_path, log, log, 1, 1)
    assert details["reward_txt"] == "0.000000" and details["tamper_check"] == "passed"


def test_wrong_result_matching_mutant_preserves_partial_reward(tmp_path: Path):
    other = "t.py::test_other PASSED\nt.py::test_a PASSED\n"
    details = _main(
        tmp_path,
        "t.py::test_m FAILED\n" + other,
        "t.py::test_m PASSED\n" + other,
        1,
        0,
        f2p=["t.py::test_m", "t.py::test_other"],
    )
    assert details["reward_txt"] == "0.500000"
    assert details["tamper_check"] == "passed"
    assert details["parse_status"] == "ok"
    assert "tamper_signal" not in details


def _junit(path: Path, cases: str) -> str:
    path.write_text(f'<testsuites><testsuite name="s">{cases}</testsuite></testsuites>')
    return str(path)


def _run_junit(tmp_path: Path, report: str, exit_code: int, *extra: str) -> dict:
    out = tmp_path / "out"
    verifier.main(["--junit", report, "--exit-code", str(exit_code), "--out-dir", str(out), *extra])
    return json.loads((out / "reward-details.json").read_text())


def test_junit_with_mutants(tmp_path: Path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"files": {}, "ids": ["m::test_a"], "exit_nonzero": True}))
    run_a = _junit(
        tmp_path / "a.xml",
        '<testcase classname="m" name="test_a"/><testcase classname="m" name="b"/>',
    )
    honest_b = _junit(
        tmp_path / "b.xml",
        '<testcase classname="m" name="test_a"><failure/></testcase>'
        '<testcase classname="m" name="b"/>',
    )
    extra = ["--mutants", str(spec), "--mutant-junit", honest_b, "--mutant-exit-code", "1"]
    assert _run_junit(tmp_path, run_a, 0, *extra)["reward"] == 1.0
    forged_b = _junit(
        tmp_path / "f.xml",
        '<testcase classname="m" name="test_a"/><testcase classname="m" name="b"/>',
    )
    extra = ["--mutants", str(spec), "--mutant-junit", forged_b, "--mutant-exit-code", "0"]
    details = _run_junit(tmp_path, run_a, 0, *extra)
    assert details["reward"] == 0.0 and details["tamper_signal"] == "mutant_passed"


# ---------------------------------------------------------------------------
# Two runs that code under test can't tell apart
# ---------------------------------------------------------------------------


def _two_run_script() -> str:
    from repo2rlenv.pipelines.pr_runtime import build_eval_script

    mutants = make_mutants(
        ["tests/test_x.py::test_low"],
        lambda path: "def test_low():\n    assert clamp(-5) == 0\n",
        allowed_files={"tests/test_x.py"},
        seed="s",
    )
    assert mutants is not None
    patch = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/tests/test_x.py\n@@ -0,0 +1 @@\n+x\n"
    )
    return build_eval_script(
        "a" * 40,
        patch,
        ["python -m pytest tests/test_x.py"],
        fail_to_pass=["tests/test_x.py::test_low"],
        pass_to_pass=[],
        run_timeout=120,
        mutants=mutants,
    )


def test_both_runs_call_the_launcher_identically():
    script = _two_run_script()
    launches = [line for line in script.splitlines() if "launcher.py" in line]
    assert len(launches) == 2 and launches[0] == launches[1]
    assert "mutant" not in launches[0]
    assert "$((RANDOM % 2))" in script
    assert "--mutants" in script and "--mutant-log" in script


def test_without_mutants_the_suite_runs_once():
    from repo2rlenv.pipelines.pr_runtime import build_eval_script

    script = build_eval_script(
        "a" * 40,
        "",
        ["python -m pytest"],
        fail_to_pass=["t::x"],
        pass_to_pass=[],
    )
    assert sum("launcher.py" in line for line in script.splitlines()) == 1
    assert "--mutants" not in script


def test_settle_rewrites_every_hidden_file_with_one_mtime(tmp_path: Path):
    from repo2rlenv.pipelines import _grader_tools as tools

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.py").write_text("original a\n")
    (tmp_path / "tests" / "b.py").write_text("original b\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"files": {"tests/a.py": "mutant a\n"}, "ids": ["x"]}))
    paths = ["tests/a.py", "tests/b.py", "tests/deleted.py"]
    tools.settle(str(tmp_path), paths)
    assert (tmp_path / "tests" / "a.py").read_text() == "original a\n"
    tools.settle(str(tmp_path), paths, str(spec), mutated=True, mtime=1_900_000_000)
    assert (tmp_path / "tests" / "a.py").read_text() == "mutant a\n"
    assert (tmp_path / "tests" / "b.py").read_text() == "original b\n"
    mtimes = {os.stat(tmp_path / "tests" / name).st_mtime for name in ("a.py", "b.py")}
    assert mtimes == {1_900_000_000}


def test_settle_refuses_a_symlinked_test_file(tmp_path: Path):
    from repo2rlenv.pipelines import _grader_tools as tools

    (tmp_path / "real.py").write_text("x\n")
    (tmp_path / "t.py").symlink_to(tmp_path / "real.py")
    with pytest.raises(ValueError):
        tools.settle(str(tmp_path), ["t.py"])


def test_keep_copies_only_a_regular_report(tmp_path: Path):
    from repo2rlenv.pipelines import _grader_tools as tools

    report = tmp_path / "r.xml"
    report.write_text("<testsuite/>")
    tools.keep(str(report), str(tmp_path / "kept.xml"), 1024)
    assert (tmp_path / "kept.xml").read_text() == "<testsuite/>"
    os.mkfifo(tmp_path / "fifo.xml")
    (tmp_path / "link.xml").symlink_to(report)
    for name in ("fifo.xml", "link.xml", "missing.xml"):
        tools.keep(str(tmp_path / name), str(tmp_path / f"kept-{name}"), 1024)
        assert not (tmp_path / f"kept-{name}").exists()
    tools.keep(str(report), str(tmp_path / "small.xml"), 4)
    assert not (tmp_path / "small.xml").exists()


def test_validation_swaps_mutants_in_the_way_grading_does():
    from repo2rlenv.pipelines.pr_runtime_validate import _MUTANTS, _build_stage_script

    patch = "diff --git a/tests/t.py b/tests/t.py\n--- a/tests/t.py\n+++ b/tests/t.py\n"
    script = _build_stage_script(
        "a" * 40,
        apply_patch="x",
        apply_test_patch=patch,
        test_cmds=["pytest"],
        staged={_MUTANTS: "/opt/r2e-stage/0.b64"},
    )
    assert "settle --root /workspace --path tests/t.py" in script
    assert "--mutated" in script


def test_claimed_check_runs_b_unless_a_plainly_passed_no_mutant(tmp_path: Path, capsys):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"files": {}, "ids": ["t.py::test_a"], "exit_nonzero": True}))

    def claimed(log: str) -> str:
        (tmp_path / "a.log").write_text(log)
        verifier.main(
            [
                "--claimed",
                "--log",
                str(tmp_path / "a.log"),
                "--test-cmds",
                "pytest",
                "--mutants",
                str(spec),
            ]
        )
        return capsys.readouterr().out.strip()

    assert claimed("t.py::test_a PASSED\nt.py::test_b PASSED\n") == "1"
    assert claimed("t.py::test_a FAILED\nt.py::test_b PASSED\n") == "0"
    assert claimed("garbage\n") == "1"  # unreadable: run B


def test_a_mutant_run_timeout_is_a_zero_but_not_tampered(tmp_path: Path):
    details = _main(tmp_path, PASS_A, "", 0, 124, "--mutant-timed-out", "1")
    assert details["reward_txt"] == "0.000000"
    assert details["parse_status"] == "mutant_timeout"
    assert details["tamper_check"] == "inconclusive"
    assert details["evaluation_status"] == "graded"


def test_purge_never_walks_the_grader_directories(monkeypatch):
    from repo2rlenv.pipelines import _grader_launcher as launcher

    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    launcher._owned_paths(20001, ["/workspace"], roots=["/"])
    pruned = [seen[0][i + 1] for i, part in enumerate(seen[0]) if part == "-path"]
    # Each run's tree is kept there; walking it would let a run that fills its
    # tree stall the next run's cleanup into an ungraded run.
    assert {"/opt/r2e", "/opt/r2e-validate", "/.r2e-agent-tree-*", "/workspace"} <= set(pruned)


def test_cleanup_failing_before_the_tests_start_is_reported_apart(monkeypatch, capsys):
    from repo2rlenv.pipelines import _grader_launcher as launcher

    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    monkeypatch.setattr(launcher, "unsafe_image_paths", lambda uid: [])
    monkeypatch.setattr(launcher, "_ensure_account", lambda: None)

    def stalled(*args, **kwargs):
        raise RuntimeError("deleting the test user's files took over 120 s")

    monkeypatch.setattr(launcher, "_cleanup", stalled)
    assert launcher.main(["--log", "/nonexistent/log", "--path", "/bin", "--purge", "true"]) == 0
    assert capsys.readouterr().out.strip() == "precleanup_failed"


def test_cleanup_failing_before_the_second_run_is_a_graded_zero():
    script = _two_run_script()
    case = next(line for line in script.splitlines() if "precleanup_failed)" in line)
    # After an earlier run, what is left is that run's doing: graded 0.
    assert '"${R2E_RUNS:-0}" -gt 1' in case
    assert "r2e_finish graded test_cleanup_failed" in case
    assert "else r2e_finish ungraded grader_launch_failed" in case


def test_a_full_disk_while_preparing_the_runs_is_a_graded_zero():
    script = _two_run_script()
    assert "No space left" in script and "r2e_finish graded submission_no_space" in script


def test_the_single_run_settles_the_hidden_tests_too():
    from repo2rlenv.pipelines.pr_runtime import build_eval_script

    patch = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/tests/test_x.py\n@@ -0,0 +1 @@\n+x\n"
    )
    script = build_eval_script(
        "a" * 40, patch, ["python -m pytest"], fail_to_pass=["tests/test_x.py::t"], pass_to_pass=[]
    )
    assert 'settle --root /workspace --path tests/test_x.py --mtime "$R2E_MTIME"' in script
    assert "--mutated" not in script
