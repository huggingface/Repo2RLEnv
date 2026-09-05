from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest

from repo2rlenv.curation import probe_runtime


def test_long_training_logs_keep_final_exception_within_original_bound():
    data = (
        b"initial warning\n" + b"loading weights progress\n" * 2_000 + b"OSError: File too large\n"
    )
    result = probe_runtime._stderr_excerpt(BytesIO(data))
    assert result.startswith("initial warning\n")
    assert result.endswith("OSError: File too large\n")
    assert "[stderr middle omitted]" in result
    assert len(result.encode()) == 16_000


def test_short_stderr_is_preserved_including_non_utf8_diagnostics():
    assert probe_runtime._stderr_excerpt(BytesIO(b"error\n")) == "error\n"
    assert probe_runtime._stderr_excerpt(BytesIO(b"\xfftail")) == "\ufffdtail"
    assert probe_runtime._stderr_excerpt(BytesIO()) == ""


def test_failed_probe_reports_cause_after_progress_output_without_executing_code(monkeypatch):
    def popen(*args, **kwargs):
        def communicate(data, timeout):
            kwargs["stderr"].write(
                b"progress\n" * 3_000 + b"FinalFailure: checkpoint exceeds limit"
            )

        return SimpleNamespace(communicate=communicate, returncode=1)

    monkeypatch.setattr(probe_runtime.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="FinalFailure: checkpoint exceeds limit"):
        probe_runtime.run_probe("print(1)")
