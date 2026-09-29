"""Standalone Linux verifier: private scoring, unprivileged bounded submissions.

Copied into Harbor tasks. Requires only Python's standard library. Infrastructure
errors exit without a reward; malformed, missing or failing submissions score zero.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def limits():
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def finite_reward(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Scorer must return a finite numeric reward in [0,1]")
    return float(value)


def run_solution(path, instance, directory):
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            [sys.executable, "-I", str(path)],
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd=directory,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            user=65534,
            group=65534,
            extra_groups=[],
            start_new_session=True,
            preexec_fn=limits,
        )
        try:
            process.communicate(json.dumps(instance).encode(), timeout=5)
        except subprocess.TimeoutExpired:
            return None, "timeout"
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if process.returncode != 0:
            return None, "program_error"
        stdout.seek(0)
        output = stdout.read(1024 * 1024 + 1)
        if len(output) > 1024 * 1024:
            return None, "output_limit"
        try:
            parsed = json.loads(output, parse_constant=lambda _: None)
        except (ValueError, UnicodeDecodeError):
            return None, "invalid_json"
        if not isinstance(parsed, dict):
            return None, "invalid_shape"
        return parsed, "completed"


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Verifier requires root to isolate submitted code")
    tests = Path(__file__).resolve().parent
    tests.chmod(0o700)
    if Path("/solution").exists():
        Path("/solution").chmod(0o700)
    log = Path("/logs/verifier")
    log.mkdir(parents=True, exist_ok=True)
    for filename in ("reward.txt", "reward.json", "scores.json"):
        (log / filename).unlink(missing_ok=True)
    contract = json.loads((tests / "contract.json").read_text())
    generator = load(tests / "generator.py", "private_generator")
    scorer = load(tests / "scorer.py", "private_scorer")
    cases = generator.generate(contract["seed"])
    if (
        not isinstance(cases, list)
        or not 8 <= len(cases) <= 16
        or any(type(c) is not dict for c in cases)
    ):
        raise ValueError("Generator must return 8-16 JSON objects")
    serialized = json.dumps(cases, sort_keys=True, allow_nan=False)
    if len(serialized.encode()) > 2 * 1024 * 1024:
        raise ValueError("Generated test data exceeds 2 MiB")
    if serialized != json.dumps(
        generator.generate(contract["seed"]), sort_keys=True, allow_nan=False
    ):
        raise ValueError("Generator is not deterministic")
    for case in cases:
        for invalid in (None, True, [], {}, {"__invalid__": True}):
            if finite_reward(scorer.score(case, invalid)) != 0:
                raise ValueError("Scorer accepts malformed output")

    solution = Path("/workspace/solution.py")
    code = None
    try:
        fd = os.open(solution, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (FileNotFoundError, OSError):
        fd = None
    if fd is not None:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_size <= 65536:
                code = stream.read(65537)
    rows = []
    with tempfile.TemporaryDirectory(prefix="frontiersmith-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        submitted = directory / "solution.py"
        if code is not None:
            submitted.write_bytes(code)
            submitted.chmod(0o444)
        for index, case in enumerate(cases):
            output, status = (
                run_solution(submitted, case, directory)
                if code is not None
                else (None, "missing_solution")
            )
            score = finite_reward(scorer.score(case, output)) if status == "completed" else 0.0
            if status == "completed" and score != finite_reward(scorer.score(case, output)):
                raise ValueError("Scorer is not deterministic")
            rows.append({"case": index, "reward": score, "status": status})
    reward = sum(row["reward"] for row in rows) / len(rows)
    (log / "scores.json").write_text(json.dumps({"reward": reward, "cases": rows}, indent=2))
    (log / "reward.txt").write_text(str(reward))
    (log / "reward.json").write_text(json.dumps({"reward": reward}))
    print(json.dumps({"reward": reward, "case_count": len(rows)}))


if __name__ == "__main__":
    main()
