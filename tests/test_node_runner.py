"""Node test commands are frozen against base at generation."""

from __future__ import annotations

import json

from repo2rlenv.pipelines._node_runner import freeze_node_commands


def _reader(files: dict[str, str]):
    return lambda path: files.get(path)


def _package(**fields) -> str:
    return json.dumps({"name": "pkg", **fields})


def test_npm_test_becomes_the_base_script_with_pre_and_post_and_jest_flags():
    files = {
        "package.json": _package(
            scripts={"pretest": "node build.js", "test": "jest --runInBand", "posttest": "echo ok"},
            jest={"testEnvironment": "node"},
        )
    }
    [cmd] = freeze_node_commands(["npm test -- src/a.test.js"], _reader(files))
    assert cmd == (
        'export PATH="$PWD/node_modules/.bin:$PATH" && ( node build.js ) && '
        "( jest --config package.json --ci --runInBand src/a.test.js ) && ( echo ok )"
    )


def test_jest_config_file_wins_and_npx_calls_are_flagged():
    files = {"package.json": _package(), "jest.config.js": "module.exports = {}"}
    [cmd] = freeze_node_commands(["npx jest src"], _reader(files))
    assert "npx jest --config jest.config.js --ci src" in cmd


def test_nested_npm_run_is_resolved():
    files = {"package.json": _package(scripts={"test": "npm run unit", "unit": "jest"})}
    [cmd] = freeze_node_commands(["npm test"], _reader(files))
    assert "npm run" not in cmd and "jest --ci" in cmd


def test_mocha_drops_config_only_when_base_has_none():
    bare = {"package.json": _package(scripts={"test": "mocha test/"})}
    [cmd] = freeze_node_commands(["npm test"], _reader(bare))
    assert "mocha --no-config --no-package test/" in cmd
    configured = {**bare, ".mocharc.yml": "require: ts-node/register\n"}
    [cmd] = freeze_node_commands(["npm test"], _reader(configured))
    assert "--no-config" not in cmd and "mocha test/" in cmd


def test_vitest_gets_the_base_config_file():
    files = {"package.json": _package(scripts={"test": "vitest run"}), "vite.config.ts": "x"}
    [cmd] = freeze_node_commands(["yarn test"], _reader(files))
    assert "vitest --config vite.config.ts run" in cmd


def test_non_node_and_unknown_scripts_are_left_alone():
    assert freeze_node_commands(["pytest -q"], _reader({})) == ["pytest -q"]
    files = {"package.json": _package(scripts={})}
    [cmd] = freeze_node_commands(["yarn install && npm run missing"], _reader(files))
    assert "yarn install" in cmd and "npm run missing" in cmd


def test_config_flags_are_skipped_after_a_directory_change():
    files = {"package.json": _package(jest={})}
    [cmd] = freeze_node_commands(["cd web && npx jest"], _reader(files))
    assert "--config" not in cmd and "--ci" in cmd
