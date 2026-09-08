from __future__ import annotations

import argparse
import builtins
import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from repo2rlenv import cli as main_cli
from repo2rlenv.tasksmith import cli
from repo2rlenv.tasksmith.config import TasksmithConfig


@pytest.fixture
def files(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "ledger_path": str(tmp_path / "existing-ledger.json"),
                "ledger_limit_usd": 380,
                "campaign_id": "tasksmith-test",
                "campaign_limit_usd": 40,
            }
        )
    )
    panel_path = tmp_path / "panel.json"
    panel_path.write_text(
        json.dumps([f"https://github.com/example/library/pull/{i}" for i in range(1, 6)])
    )
    display = Mock()
    monkeypatch.setattr(cli.console, "kv", display)
    return config_path, panel_path, tmp_path / "untouched-output", display


def arguments(files, *extra):
    config, panel, out, _ = files
    parser = argparse.ArgumentParser()
    cli.register(parser.add_subparsers(required=True))
    return parser.parse_args(
        ["tasksmith", "--config", str(config), "--panel", str(panel), "--out", str(out), *extra]
    )


def fake_module(monkeypatch, name, function, result):
    module = ModuleType(name)
    runner = AsyncMock(return_value=result)
    setattr(module, function, runner)
    monkeypatch.setitem(sys.modules, name, module)
    return runner


def test_static_five_pr_plan_never_imports_execution_modules_or_mutates_ledger(files, monkeypatch):
    config, panel, out, display = files
    ledger = json.loads(config.read_text())["ledger_path"]
    from pathlib import Path

    Path(ledger).write_text('{"entries":{"old":{"charged_usd":12.5,"status":"reserved"}}}')
    original = Path(ledger).read_bytes()
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name in {
            "repo2rlenv.tasksmith.pipeline",
            "repo2rlenv.tasksmith.conformance",
            "modal",
            "daytona",
            "harbor",
        }:
            raise AssertionError(f"Static plan imported paid execution module: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert cli.cmd_tasksmith(arguments(files, "--plan")) == 0
    summary = display.call_args.args[0]
    assert summary["panel_prs"] == 5 and summary["remote_calls"] == 0
    assert "not evaluated" in summary["mode"]
    assert not out.exists()
    assert Path(ledger).read_bytes() == original
    assert panel.exists()


def test_research_panel_preserves_provenance_and_counts_canonical_sources(files):
    _, panel, _, display = files
    rows = json.loads(panel.read_text())
    panel.write_text(
        json.dumps(
            {
                "status": "research",
                "panel": [
                    {"url": url, "head_sha": "a" * 40, "hazards": ["Inspect exact source"]}
                    for url in rows
                ],
            }
        )
    )
    original = panel.read_bytes()
    assert cli.cmd_tasksmith(arguments(files, "--plan")) == 0
    assert display.call_args.args[0]["panel_prs"] == 5
    assert panel.read_bytes() == original


@pytest.mark.parametrize(
    "rows",
    [
        [],
        ["https://github.com/example/library/pull/1", "https://github.com/EXAMPLE/Library/pull/01"],
        [{"title": "missing URL"}],
        ["https://other.invalid/repo/pull/1"],
    ],
)
def test_invalid_or_duplicate_panel_never_launches(files, monkeypatch, rows):
    _, panel, _, _ = files
    panel.write_text(json.dumps(rows))
    runner = fake_module(monkeypatch, "repo2rlenv.tasksmith.pipeline", "run_panel", {})
    with pytest.raises(ValueError):
        cli.cmd_tasksmith(arguments(files))
    runner.assert_not_awaited()


def test_unknown_configuration_fields_fail_before_remote_execution(files, monkeypatch):
    config, _, _, _ = files
    data = json.loads(config.read_text()) | {"reset_previous_spend": True}
    config.write_text(json.dumps(data))
    runner = fake_module(monkeypatch, "repo2rlenv.tasksmith.pipeline", "run_panel", {})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        cli.cmd_tasksmith(arguments(files))
    runner.assert_not_awaited()


@pytest.mark.parametrize("status, code", [("completed", 0), ("incomplete", 1)])
def test_default_run_dispatches_exact_config_panel_and_output_without_retry_flag(
    files, monkeypatch, status, code
):
    config, panel, out, display = files
    runner = fake_module(
        monkeypatch, "repo2rlenv.tasksmith.pipeline", "run_panel", {"status": status, "accepted": 0}
    )
    assert cli.cmd_tasksmith(arguments(files)) == code
    runner.assert_awaited_once_with(
        TasksmithConfig.model_validate_json(config.read_text()), panel, out
    )
    assert display.call_args.args[0]["accepted"] == 0


@pytest.mark.parametrize("passed, code", [(True, 0), (False, 1)])
def test_provider_checks_are_explicit_and_do_not_generate_tasks(files, monkeypatch, passed, code):
    config, _, out, _ = files
    runner = fake_module(
        monkeypatch,
        "repo2rlenv.tasksmith.conformance",
        "check_providers",
        {"passed": passed, "scope": "CPU fixture only"},
    )
    pipeline = fake_module(monkeypatch, "repo2rlenv.tasksmith.pipeline", "run_panel", {})
    parser = argparse.ArgumentParser()
    cli.register(parser.add_subparsers(required=True))
    args = parser.parse_args(
        ["tasksmith", "--config", str(config), "--out", str(out), "--check-providers"]
    )
    assert cli.cmd_tasksmith(args) == code
    runner.assert_awaited_once_with(TasksmithConfig.model_validate_json(config.read_text()), out)
    pipeline.assert_not_awaited()


def test_plan_cannot_be_combined_with_paid_provider_checks(files):
    with pytest.raises(SystemExit) as error:
        arguments(files, "--plan", "--check-providers")
    assert error.value.code == 2


def test_top_level_cli_registers_tasksmith(files, monkeypatch):
    config, panel, out, display = files
    monkeypatch.setattr(main_cli, "_load_dotenv_if_present", lambda: None)
    assert (
        main_cli.main(
            [
                "tasksmith",
                "--config",
                str(config),
                "--panel",
                str(panel),
                "--out",
                str(out),
                "--plan",
            ]
        )
        == 0
    )
    assert display.call_args.args[0]["panel_prs"] == 5
