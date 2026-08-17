"""Shared fakes for the `env_setup` pipeline's unit tests.

`_FakeSandbox` and `_bootstrap` are used by both `tests/test_setup_recipe.py`
(Task 9) and `tests/test_env_setup.py` (Task 10) — defined once here so
neither test module duplicates them.
"""

from __future__ import annotations


def _bootstrap(**kw):
    from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint

    defaults = dict(
        image_digest="sha256:x",
        image_tag="t",
        language=LanguageHint.PYTHON,
        repo="pallets/click",
        ref="a" * 40,
        rebuild_cmds=[],
        test_cmds=["python -m pytest -v"],
        smoke_passed=True,
        iterations=3,
        build_time_sec=1.0,
        llm_provider="anthropic/claude-sonnet-4-6",
        verify_passed=True,
    )
    defaults.update(kw)
    return BootstrapResult(**defaults)


class _FakeSandbox:
    """Records the scripts it was asked to run and replays canned results."""

    def __init__(self, results):
        self._results = list(results)
        self.scripts = []
        self.files = {}
        self.closed = False

    def exec(self, script, *, timeout):
        from repo2rlenv.bootstrap.docker import ExecResult

        self.scripts.append(script)
        exit_code, out = self._results.pop(0)
        return ExecResult(exit_code=exit_code, stdout=out, stderr="", duration_sec=0.1)

    def put_files(self, files, dest_dir):
        self.files.update(files)

    def close(self):
        self.closed = True
