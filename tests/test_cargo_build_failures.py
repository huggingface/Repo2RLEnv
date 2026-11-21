"""Target-scoped Cargo failures must agree at generation and grading time."""

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

_COMMAND = "cargo test --no-fail-fast --message-format=json"
_PACKAGE = "path+file:///workspace#mycrate@0.1.0"


def _target(name, kind):
    return {"kind": [kind], "crate_types": [kind], "name": name, "src_path": f"/w/{name}.rs"}


def _artifact(name, kind="test", package=_PACKAGE, executable=None):
    message = {"reason": "compiler-artifact", "package_id": package, "target": _target(name, kind)}
    message["executable"] = executable or f"/workspace/target/debug/deps/{name}-0123abcd"
    return json.dumps(message) + "\n"


def _error(name, kind="test", package=_PACKAGE, level="error"):
    return (
        json.dumps(
            {
                "reason": "compiler-message",
                "package_id": package,
                "target": _target(name, kind),
                "message": {"level": level, "message": "cannot find function `new`"},
            }
        )
        + "\n"
    )


def _finished(success):
    return json.dumps({"reason": "build-finished", "success": success}) + "\n"


def _running(name, source=None):
    return f"     Running {source or f'tests/{name}.rs'} (target/debug/deps/{name}-0123abcd)\n"


def _parse(log):
    generated = parse_logs([_COMMAND], log)
    assert generated == verifier.parse_logs("cargo", log)
    return generated


def test_explicit_build_failure_is_scoped_to_target():
    log = _error("broken") + _finished(False)
    assert _parse(log) == {"mycrate/test/broken": "ERROR"}


def test_same_named_tests_do_not_collide_across_targets():
    log = (
        _artifact("mycrate", "lib")
        + _artifact("working")
        + _finished(True)
        + _running("mycrate", "unittests src/lib.rs")
        + "test tests::parse ... FAILED\n"
        + _running("working")
        + "test tests::parse ... ok\n"
    )
    assert _parse(log) == {
        "mycrate/lib/mycrate::tests::parse": "FAILED",
        "mycrate/test/working::tests::parse": "PASSED",
    }


def test_compiler_error_without_a_failed_build_is_not_evidence():
    assert _parse(_error("broken") + _finished(True)) == {}
    assert _parse(_error("broken", level="warning") + _finished(False)) == {}
    assert _parse(_finished(False)) == {}


def test_test_output_cannot_move_a_later_binarys_results():
    log = (
        _artifact("first")
        + _artifact("second")
        + _finished(True)
        + _running("first")
        + _artifact("elsewhere", executable="/workspace/target/debug/deps/second-0123abcd")
        + "test one ... ok\n"
        + _running("second")
        + "test two ... FAILED\n"
    )
    assert _parse(log) == {
        "mycrate/test/first::one": "PASSED",
        "mycrate/test/second::two": "FAILED",
    }


def test_repeated_command_build_failure_invalidates_earlier_pass():
    log = (
        _artifact("broken")
        + _finished(True)
        + _running("broken")
        + "test parse ... ok\n"
        + _error("broken")
        + _finished(False)
    )
    assert _parse(log) == {"mycrate/test/broken": "ERROR", "mycrate/test/broken::parse": "ERROR"}


@pytest.mark.parametrize(
    ("package", "name"),
    [
        ("path+file:///w/crates/foo#foo@0.1.0", "foo"),
        ("path+file:///w/crates/foo#0.1.0", "foo"),
        ("foo 0.1.0 (path+file:///w/crates/foo)", "foo"),
    ],
)
def test_package_ids_of_every_cargo_format_name_the_package(package, name):
    log = _artifact("api", package=package) + _running("api") + "test parse ... ok\n"
    assert _parse(log) == {f"{name}/test/api::parse": "PASSED"}


def test_doctests_are_keyed_under_their_library():
    log = (
        _artifact("my_crate", "lib", package="path+file:///w#my-crate@0.1.0")
        + "   Doc-tests my_crate\n"
        + "test src/lib.rs - add (line 7) ... ok\n"
    )
    assert _parse(log) == {"my-crate/doc/my_crate::src/lib.rs - add": "PASSED"}


@pytest.mark.parametrize(
    "line", ['{"reason":', "[]", '{"reason":"compiler-artifact","target":[]}', '{"reason":7}']
)
def test_malformed_messages_are_ignored(line):
    assert _parse(line + "\n" + _finished(False)) == {}


def test_legacy_text_remains_readable():
    log = _running("api") + "test parse ... ok\n"
    assert parse_logs(["cargo test"], log) == {"parse": "PASSED"}
    assert verifier.parse_logs("cargo", log) == {"parse": "PASSED"}


@pytest.mark.parametrize(
    "command",
    [
        "cargo test",
        "cargo test -q",
        "cargo test --message-format=short",
        "cargo test --message-format human",
        "cargo test --no-fail-fast --message-format=json",
        "cargo test --message-format=json --no-fail-fast",
    ],
)
def test_normalized_json_replaces_other_formats_and_is_idempotent(command):
    normalized = normalize_test_cmds_for_runtime([command])
    assert normalized == [_COMMAND]
    assert normalize_test_cmds_for_runtime(normalized) == normalized


def test_test_binary_arguments_are_preserved():
    assert normalize_test_cmds_for_runtime(["cargo test --workspace -- --message-format=x"]) == [
        f"{_COMMAND} --workspace -- --message-format=x"
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
        test_cmds=[_COMMAND],
        language="rust",
    )


def test_validation_promotes_only_tests_in_the_failed_target(monkeypatch):
    pre = _error("broken") + _finished(False)
    post = (
        _artifact("broken")
        + _artifact("working")
        + _finished(True)
        + _running("broken")
        + "test parse ... ok\n"
        + "test skipped ... ignored\n"
        + _running("working")
        + "test parse ... ok\n"
    )
    outcome = _validate(monkeypatch, pre, post)
    assert outcome.status == "verified"
    assert outcome.fail_to_pass == ["mycrate/test/broken::parse"]
    assert outcome.pass_to_pass == []
    grade = verifier.grade
    assert grade(outcome.fail_to_pass, [], _parse(post), "cargo")["reward"] == 1
    assert grade(outcome.fail_to_pass, [], _parse(pre), "cargo")["reward"] == 0


def test_flaky_post_fix_test_is_not_inferred(monkeypatch):
    pre = _error("broken") + _finished(False)
    passing = _artifact("broken") + _finished(True) + _running("broken") + "test parse ... ok\n"
    outcome = _validate(monkeypatch, pre, passing, rerun=passing.replace("... ok", "... FAILED"))
    assert outcome.status == "failed"
    assert outcome.fail_to_pass == []
    assert outcome.flaky == ["mycrate/test/broken::parse"]


@pytest.mark.skipif(shutil.which("cargo") is None, reason="Rust toolchain not installed")
def test_real_cargo_build_error_and_duplicate_test_names(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "mycrate"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    lib = "pub fn old() -> i32 { 1 }\n"
    (tmp_path / "src/lib.rs").write_text(lib, encoding="utf-8")
    for target, call in (("working", "old() == 1"), ("broken", "new() == 2")):
        (tmp_path / f"tests/{target}.rs").write_text(
            f"#[test]\nfn parse() {{ assert!(mycrate::{call}); }}\n", encoding="utf-8"
        )
    env = {
        **os.environ,
        "CARGO_NET_OFFLINE": "true",
        "CARGO_TERM_COLOR": "never",
        "CARGO_TARGET_DIR": str(tmp_path / "target"),
        "RUSTFLAGS": "",
    }
    command = shlex.split(normalize_test_cmds_for_runtime(["cargo test"])[0])

    def run():
        # The launcher folds stderr (Cargo's `Running` headers) into the log.
        return subprocess.run(
            command,
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            timeout=600,
        )

    pre = run()
    assert pre.returncode != 0
    assert _parse(pre.stdout) == {"mycrate/test/broken": "ERROR"}
    (tmp_path / "src/lib.rs").write_text(lib + "pub fn new() -> i32 { 2 }\n", encoding="utf-8")
    post = run()
    assert post.returncode == 0, post.stdout
    outcome = _validate(monkeypatch, pre.stdout, post.stdout)
    assert outcome.status == "verified"
    # Cargo runs no test when any target fails to compile, so only the failed
    # target's tests are inferred; `working::parse` has no base status.
    assert outcome.fail_to_pass == ["mycrate/test/broken::parse"]
    assert "mycrate/test/working::parse" in _parse(post.stdout)
    assert verifier.grade(outcome.fail_to_pass, [], _parse(post.stdout), "cargo")["reward"] == 1
    assert verifier.grade(outcome.fail_to_pass, [], _parse(pre.stdout), "cargo")["reward"] == 0
