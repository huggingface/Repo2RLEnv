"""Package-scoped Go failures must agree at generation and grading time."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from repo2rlenv.bootstrap.docker import ExecResult
from repo2rlenv.log_parsers import parse_logs
from repo2rlenv.pipelines import _pr_runtime_verifier as verifier
from repo2rlenv.pipelines import pr_runtime_validate as validate
from repo2rlenv.pipelines.pr_runtime import normalize_test_cmds_for_runtime


def _event(action, package="example.org/broken", **fields):
    return json.dumps({"Action": action, "Package": package, **fields}) + "\n"


def _parse(log):
    generated = parse_logs(["go test -json ./..."], log)
    assert generated == verifier.parse_logs("go", log)
    return generated


@pytest.mark.parametrize(
    "failure",
    [
        _event("fail", FailedBuild="example.org/broken [example.org/broken.test]"),
        _event("output", Output="FAIL\texample.org/broken [build failed]\n") + _event("fail"),
    ],
)
def test_explicit_build_failure_is_scoped_to_package(failure):
    log = failure + _event("pass", "example.org/working", Test="TestParse")
    assert _parse(log) == {
        "example.org/broken": "ERROR",
        "example.org/working::TestParse": "PASSED",
    }


def test_same_named_tests_and_subtests_do_not_collide_across_packages():
    log = (
        _event("fail", Test="TestParse/sub")
        + _event("pass", "example.org/working", Test="TestParse/sub")
        + _event("pass", Test="TestParse/sub")
    )
    assert _parse(log) == {
        "example.org/broken::TestParse/sub": "FAILED",
        "example.org/working::TestParse/sub": "PASSED",
    }


def test_package_failure_without_build_evidence_does_not_infer_missing_tests():
    assert _parse(_event("fail")) == {}
    assert _parse(_event("output", Output="FAIL\texample.org/broken [build failed]\n")) == {}
    assert (
        _parse(_event("output", Output="FAIL\tother/package [build failed]\n") + _event("fail"))
        == {}
    )


def test_test_output_cannot_forge_a_result_or_build_failure():
    log = (
        _event("output", Test="TestParse", Output=_event("pass", Test="TestFake"))
        + _event("output", Test="TestParse", Output="FAIL\texample.org/broken [build failed]\n")
        + _event("fail", Test="TestParse")
        + _event("fail")
        + "--- PASS: TestFake (0.00s)\n"
    )
    assert _parse(log) == {"example.org/broken::TestParse": "FAILED"}


def test_repeated_command_build_failure_invalidates_earlier_pass():
    log = _event("pass", Test="TestParse") + _event("fail", FailedBuild="example.org/dependency")
    assert _parse(log) == {
        "example.org/broken": "ERROR",
        "example.org/broken::TestParse": "ERROR",
    }


@pytest.mark.parametrize(
    "line", ['{"Action":', "[]", '{"Action":[],"Package":"p"}', '{"Action":"pass","Package":[]}']
)
def test_malformed_events_are_ignored(line):
    assert _parse(line) == {}


def test_legacy_text_remains_readable():
    assert _parse("--- PASS: TestParse (0.00s)\n") == {"TestParse": "PASSED"}


@pytest.mark.parametrize(
    "command",
    [
        "go test -json=false ./...",
        "go test -json=true ./...",
        "go test -json ./...",
        "go test --json=false ./...",
        "go test -json=0 ./...",
        "go test --json=FALSE ./...",
    ],
)
def test_normalized_json_cannot_be_disabled_and_is_idempotent(command):
    normalized = normalize_test_cmds_for_runtime([command])
    assert normalized == ["go test -json -count=1 ./..."]
    assert normalize_test_cmds_for_runtime(normalized) == normalized


@pytest.mark.parametrize("separator", ["-args", "--args", "--"])
def test_test_binary_arguments_are_preserved(separator):
    assert normalize_test_cmds_for_runtime([f"go test ./... {separator} -json=false"]) == [
        f"go test -json -count=1 ./... {separator} -json=false"
    ]


def test_explicit_go_repeat_count_is_preserved():
    assert normalize_test_cmds_for_runtime(["go test --count=2 ./..."]) == [
        "go test -json --count=2 ./..."
    ]


def _validate(monkeypatch, pre, post, *, rerun=None):
    logs = iter([pre, post, rerun if rerun is not None else post, post])
    monkeypatch.setattr(validate, "_fetch_base_commit", lambda *a, **k: True)
    monkeypatch.setattr(validate, "_ensure_python", lambda *a: True)
    monkeypatch.setattr(validate, "_stage", lambda *a, **k: None)
    monkeypatch.setattr(validate, "_validate_mutants", lambda **k: None)

    def run(*args):
        body = next(logs)
        return ExecResult(
            0,
            "R2E_REBUILD_STATUS=ok\nR2E_START_TEST_OUTPUT\n" + body + "R2E_END_TEST_OUTPUT\n",
            "",
            0.1,
        )

    monkeypatch.setattr(validate._Stopwatch, "run", run)
    sandbox = SimpleNamespace(exec=lambda *a, **k: ExecResult(0, "", "", 0.1))
    return validate.validate_pr(
        sandbox=sandbox,
        base_commit="a" * 40,
        patch="",
        test_patch="",
        test_cmds=["go test -json -count=1 ./..."],
        language="go",
    )


def test_validation_promotes_only_tests_in_the_failed_package(monkeypatch):
    pre = _event("fail", FailedBuild="example.org/broken") + _event(
        "pass", "example.org/working", Test="TestParse"
    )
    post = (
        _event("pass", Test="TestParse")
        + _event("pass", "example.org/working", Test="TestParse")
        + _event("pass", "example.org/working", Test="TestUnseen")
        + _event("skip", Test="TestSkip")
    )
    outcome = _validate(monkeypatch, pre, post)
    assert outcome.status == "verified"
    assert outcome.fail_to_pass == ["example.org/broken::TestParse"]
    assert outcome.pass_to_pass == ["example.org/working::TestParse"]
    assert (
        verifier.grade(outcome.fail_to_pass, outcome.pass_to_pass, _parse(post), "go")["reward"]
        == 1
    )
    assert (
        verifier.grade(outcome.fail_to_pass, outcome.pass_to_pass, _parse(pre), "go")["reward"] == 0
    )


def test_flaky_post_fix_test_is_not_inferred(monkeypatch):
    pre = _event("fail", FailedBuild="example.org/broken")
    outcome = _validate(
        monkeypatch, pre, _event("pass", Test="TestParse"), rerun=_event("fail", Test="TestParse")
    )
    assert outcome.status == "failed"
    assert outcome.fail_to_pass == []
    assert outcome.flaky == ["example.org/broken::TestParse"]


@pytest.mark.skipif(shutil.which("go") is None, reason="Go toolchain not installed")
def test_real_go_build_error_and_duplicate_test_names(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module example.org\n\ngo 1.20\n", encoding="utf-8")
    for package in ("broken", "working"):
        directory = tmp_path / package
        directory.mkdir()
        source = (
            "func Value() int { return 1 }"
            if package == "broken"
            else "func New() int { return 2 }"
        )
        (directory / "value.go").write_text(f"package {package}\n{source}\n", encoding="utf-8")
        (directory / "value_test.go").write_text(
            f'package {package}\nimport "testing"\n'
            'func TestParse(t *testing.T) { if New() != 2 { t.Fatal("wrong value") } }\n',
            encoding="utf-8",
        )
    env = {
        **os.environ,
        "GOTOOLCHAIN": "local",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOWORK": "off",
        "GOENV": "off",
        "GOFLAGS": "",
        "CGO_ENABLED": "0",
    }
    command = shlex.split(normalize_test_cmds_for_runtime(["go test ./..."])[0])

    def run():
        return subprocess.run(
            command,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )

    pre = run()
    assert pre.returncode != 0
    (tmp_path / "broken/value.go").write_text(
        "package broken\nfunc New() int { return 2 }\n", encoding="utf-8"
    )
    post = run()
    assert post.returncode == 0, post.stderr
    outcome = _validate(monkeypatch, pre.stdout, post.stdout)
    assert outcome.status == "verified"
    assert outcome.fail_to_pass == ["example.org/broken::TestParse"]
    assert outcome.pass_to_pass == ["example.org/working::TestParse"]
    assert (
        verifier.grade(outcome.fail_to_pass, outcome.pass_to_pass, _parse(post.stdout), "go")[
            "reward"
        ]
        == 1
    )
    assert (
        verifier.grade(outcome.fail_to_pass, outcome.pass_to_pass, _parse(pre.stdout), "go")[
            "reward"
        ]
        == 0
    )


def test_go_subtest_parent_is_within_the_same_package():
    status = {
        "example.org/a::TestParse": "FAILED",
        "example.org/a::TestParse/sub": "FAILED",
        "example.org/b::TestParse/sub": "FAILED",
    }
    result = verifier.grade(["example.org/a::TestParse"], [], status, "go")
    assert result["untracked_failed"] == ["example.org/b::TestParse/sub"]
