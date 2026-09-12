"""`--llm-endpoint` / `--llm-key-env` / `--llm-fallback` on `generate` and `bootstrap`."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from repo2rlenv import cli as cli_mod
from repo2rlenv.cli import _drop_config_llm_extras, _llm_overrides
from repo2rlenv.config import load_generation_input

ENDPOINT = "http://127.0.0.1:8000/v1"


def _args(**kwargs) -> argparse.Namespace:
    base = {"llm": None, "llm_fallback": None, "llm_endpoint": None, "llm_key_env": None}
    base.update(kwargs)
    return argparse.Namespace(**base)


def _write_config(tmp_path: Path, llm: dict | None) -> Path:
    cfg = {
        "repo": {"url": "pallets/click"},
        "pipeline": {"name": "pr_diff"},
        "output": {"destination": str(tmp_path), "org": "o", "dataset_name": "d"},
    }
    if llm is not None:
        cfg["llm"] = llm
    path = tmp_path / "gen.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


class TestLLMOverrides:
    def test_nothing_set_leaves_config_untouched(self):
        assert _llm_overrides(_args()) == {}

    def test_llm_only(self):
        assert _llm_overrides(_args(llm="anthropic/claude-sonnet-4-6")) == {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
        }

    def test_endpoint_and_key_env_ride_on_primary(self):
        out = _llm_overrides(
            _args(llm="hosted_vllm/Qwen/Qwen3.5-4B", llm_endpoint=ENDPOINT, llm_key_env="K")
        )
        assert out == {
            "provider": "hosted_vllm",
            "model": "Qwen/Qwen3.5-4B",  # split on the first slash only
            "endpoint": ENDPOINT,
            "api_key_env": "K",
        }

    def test_fallback_does_not_inherit_endpoint(self):
        out = _llm_overrides(
            _args(llm="openai/local", llm_fallback="anthropic/claude", llm_endpoint=ENDPOINT)
        )
        assert out["endpoint"] == ENDPOINT
        assert out["fallback"] == {"provider": "anthropic", "model": "claude"}

    def test_fallback_composes_with_config_like_endpoint_does(self):
        out = _llm_overrides(_args(llm_fallback="anthropic/claude", config="x.yaml"))
        assert out == {"fallback": {"provider": "anthropic", "model": "claude"}}

    @pytest.mark.parametrize("flag", ["llm_fallback", "llm_endpoint", "llm_key_env"])
    def test_secondary_flags_need_llm_or_config(self, flag):
        with pytest.raises(SystemExit, match="need --llm"):
            _llm_overrides(_args(**{flag: "anthropic/x" if flag == "llm_fallback" else "v"}))

    @pytest.mark.parametrize("flag", ["llm", "llm_fallback"])
    def test_provider_model_shape_enforced(self, flag):
        kwargs = {"llm": "anthropic/x"} if flag == "llm_fallback" else {}
        kwargs[flag] = "no-slash"
        with pytest.raises(SystemExit, match="expects provider/model"):
            _llm_overrides(_args(**kwargs))


def test_endpoint_overrides_config_llm_block(tmp_path: Path):
    cfg = _write_config(tmp_path, {"provider": "hosted_vllm", "model": "Qwen/Qwen3.5-4B"})
    overrides = {"llm": _llm_overrides(_args(llm_endpoint=ENDPOINT, config=str(cfg)))}
    gen = load_generation_input(cfg, overrides)
    assert gen.llm is not None
    assert (gen.llm.provider, gen.llm.model, gen.llm.endpoint) == (
        "hosted_vllm",
        "Qwen/Qwen3.5-4B",
        ENDPOINT,
    )


class TestParser:
    """Real argparse tree, command body swapped for a capture."""

    def test_generate_accepts_flags(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        seen: dict = {}
        monkeypatch.setattr(cli_mod, "cmd_generate", lambda a: seen.update(vars(a)) or 0)
        rc = cli_mod.main(
            [
                "generate",
                "--repo", "pallets/click",
                "--pipeline", "pr_diff",
                "--llm", "openai/Qwen/Qwen3.5-4B",
                "--llm-endpoint", ENDPOINT,
                "--llm-key-env", "MY_KEY",
                "--out", str(tmp_path),
            ]
        )  # fmt: skip
        assert rc == 0
        assert seen["llm_endpoint"] == ENDPOINT
        assert seen["llm_key_env"] == "MY_KEY"

    def test_bootstrap_accepts_flags(self, monkeypatch: pytest.MonkeyPatch):
        seen: dict = {}
        monkeypatch.setattr(cli_mod, "cmd_bootstrap", lambda a: seen.update(vars(a)) or 0)
        rc = cli_mod.main(
            [
                "bootstrap",
                "--repo", "pallets/click",
                "--llm", "hosted_vllm/Qwen/Qwen3.5-4B",
                "--llm-endpoint", ENDPOINT,
            ]
        )  # fmt: skip
        assert rc == 0
        assert seen["llm_endpoint"] == ENDPOINT
        assert seen["llm_key_env"] is None
        assert "llm_fallback" not in seen


class TestDropConfigLLMExtras:
    @staticmethod
    def _load(cfg: Path, **flags) -> tuple:
        args = _args(config=str(cfg), **flags)
        gen = load_generation_input(cfg, {"llm": _llm_overrides(args)})
        return gen, args

    def test_new_model_drops_config_endpoint_and_key_env(self, tmp_path: Path):
        cfg = _write_config(
            tmp_path,
            {"provider": "hosted_vllm", "model": "Qwen", "endpoint": ENDPOINT, "api_key_env": "K"},
        )
        gen, args = self._load(cfg, llm="anthropic/claude-sonnet-4-6")
        assert gen.llm.endpoint == ENDPOINT  # what a plain merge leaves behind
        gen = _drop_config_llm_extras(gen, args, cfg)
        assert (gen.llm.provider, gen.llm.model) == ("anthropic", "claude-sonnet-4-6")
        assert gen.llm.endpoint is None
        assert gen.llm.api_key_env is None

    def test_same_model_keeps_config_endpoint(self, tmp_path: Path):
        cfg = _write_config(
            tmp_path, {"provider": "hosted_vllm", "model": "Qwen", "endpoint": ENDPOINT}
        )
        gen, args = self._load(cfg, llm="hosted_vllm/Qwen")
        gen = _drop_config_llm_extras(gen, args, cfg)
        assert gen.llm.endpoint == ENDPOINT

    def test_cli_endpoint_survives_model_change(self, tmp_path: Path):
        cfg = _write_config(
            tmp_path, {"provider": "hosted_vllm", "model": "Qwen", "endpoint": "http://old/v1"}
        )
        gen, args = self._load(cfg, llm="openai/other", llm_endpoint=ENDPOINT)
        gen = _drop_config_llm_extras(gen, args, cfg)
        assert gen.llm.endpoint == ENDPOINT


def test_missing_llm_block_is_a_clean_error(tmp_path: Path):
    """No `llm:` in config, no --llm → field errors, not a traceback."""
    cfg = _write_config(tmp_path, llm=None)
    rc = cli_mod.main(["--no-ui", "generate", "--config", str(cfg), "--llm-endpoint", ENDPOINT])
    assert rc == 2
