"""Copied into the protected grader; submitted code only runs in a worker."""

from __future__ import annotations

import ast
import json
import os
import resource
import signal
import subprocess
import tempfile


def run_probe(code: str, payload=None, timeout: int = 60):
    """Run an operation against submitted code, returning JSON for trusted assertions.

    The operation must compute observations, never decide whether a test passed.
    Its globals include ``payload``, ``json``, and ``sys``. It prints one JSON value.
    """
    if any(isinstance(node, ast.Assert) for node in ast.walk(ast.parse(code))):
        raise ValueError("Assertions belong in the protected test, outside run_probe")
    if not 1 <= timeout <= 120:
        raise ValueError("Probe timeout must be between 1 and 120 seconds")
    program = "import json, sys\npayload = json.load(sys.stdin)\n" + code
    data = json.dumps(payload, allow_nan=False).encode()
    if len(data) > 1_000_000 or len(program) > 100_000:
        raise ValueError("Probe input exceeds its bounded protocol")

    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (2_000_000, 2_000_000))
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/agent",
        "PYTHONHASHSEED": "0",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "1",
    }
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            [
                "/usr/sbin/runuser",
                "-u",
                "agent",
                "--",
                "/usr/local/bin/python",
                "-I",
                "-c",
                program,
            ],
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd="/workspace",
            env=env,
            start_new_session=True,
            preexec_fn=limits,
        )
        try:
            process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise TimeoutError(f"Submitted operation exceeded {timeout}s") from None
        stderr.seek(0)
        errors = stderr.read(16000).decode(errors="replace")
        if process.returncode:
            raise RuntimeError(f"Submitted operation exited {process.returncode}: {errors}")
        stdout.seek(0)
        output = stdout.read(1_000_001)
        if len(output) > 1_000_000:
            raise ValueError("Submitted operation output exceeds 1 MB")
        try:
            return json.loads(output, parse_constant=lambda value: _nonfinite(value))
        except (ValueError, UnicodeError) as exc:
            raise ValueError(
                f"Submitted operation did not return valid JSON: {exc}; {errors}"
            ) from exc


def _nonfinite(value):
    raise ValueError(f"Non-finite observation: {value}")
