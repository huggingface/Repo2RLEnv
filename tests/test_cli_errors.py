"""User-facing failures preserve parseable output across command families."""

from __future__ import annotations

import json
import logging

import pytest

from repo2rlenv.cli import main


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    monkeypatch.setattr("repo2rlenv.cli._load_dotenv_if_present", lambda: None)


@pytest.mark.parametrize(
    "args",
    [
        ["generate", "--config", "missing.yaml"],
        ["tasksmith", "show", "missing.json"],
        ["quality", "show", "missing.json"],
        ["tasks", "show", "missing-task"],
        ["generate", "--pipeline-opt", "invalid"],
        ["generate", "--max-spend-usd", "invalid"],
        ["pipelines", "describe", "terminal_synth", "--recipe", "missing"],
        ["pipelines", "describe"],
    ],
)
def test_errors_are_json_records(args, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main([*args, "--json"]) == 2
    result = capsys.readouterr()
    assert set(json.loads(result.out)) == {"error", "message"}
    assert "Traceback" not in result.err


@pytest.mark.parametrize(
    "error", [ImportError("Install repo2rlenv[tasksmith]"), RuntimeError("Provider unavailable")]
)
@pytest.mark.parametrize("verbose", [False, True])
def test_dependency_and_provider_errors(monkeypatch, capsys, error, verbose):
    def fail(args):
        raise error

    monkeypatch.setattr("repo2rlenv.cli.cmd_generate", fail)
    assert main((["--verbose"] if verbose else []) + ["generate", "--json"]) == 2
    result = capsys.readouterr()
    assert json.loads(result.out) == {"error": type(error).__name__, "message": str(error)}
    assert ("Traceback" in result.err) == verbose


def test_human_error_does_not_pollute_stdout(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["generate", "--config", "missing.yaml"]) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert "FileNotFoundError" in result.err


def test_logs_stay_out_of_json_stream(monkeypatch, capsys):
    def fail(args):
        logging.getLogger("repo2rlenv").warning("Diagnostic before failure")
        raise RuntimeError("Provider unavailable")

    monkeypatch.setattr("repo2rlenv.cli.cmd_generate", fail)
    assert main(["generate", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "RuntimeError"
