"""Standalone trusted entry point; copied into each separate verifier image.

No Repo2RLEnv installation is needed to grade an exported task. Submitted Python
runs as an unprivileged user; this parent owns the result parser and reward file.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    directory = Path("/tests")
    parser_spec = importlib.util.spec_from_file_location(
        "test_results", directory / "test_results.py"
    )
    parser = importlib.util.module_from_spec(parser_spec)
    sys.modules[parser_spec.name] = parser
    parser_spec.loader.exec_module(parser)
    contract = json.loads((directory / "contract.json").read_text())
    logs = Path("/logs/verifier")
    logs.mkdir(parents=True, exist_ok=True)
    logs.chmod(0o755)
    reward = logs / "reward.txt"
    reward.write_text("0\n")
    reward.chmod(0o644)
    for relative in contract["submitted_files"]:
        path = Path("/workspace") / relative
        # Artifact uploads are data. Refuse link traversal and special files
        # before the child imports any learner-controlled code.
        for component in (path, *path.parents):
            if component.is_symlink():
                raise ValueError("Submitted source contains a symlink")
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Submitted source is not a bounded regular file")
        os.chown(path, 0, 0)
        path.chmod(0o644)
    with tempfile.TemporaryDirectory(prefix="r2e-grade-") as temporary:
        working = Path(temporary)
        os.chown(working, 1001, 1001)
        working.chmod(0o700)
        report = working / "results.xml"
        command = [
            "/usr/local/bin/python",
            "-I",
            "/tests/test_driver.py",
            *contract["test_paths"],
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
        ]
        with (logs / "stdout.txt").open("w") as stdout, (logs / "stderr.txt").open("w") as stderr:
            process = subprocess.Popen(
                command,
                cwd="/workspace",
                stdout=stdout,
                stderr=stderr,
                user=1001,
                group=1001,
                extra_groups=[],
                start_new_session=True,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": temporary,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                },
            )
            try:
                code = process.wait(timeout=contract["timeout_sec"])
            finally:
                # Terminate descendants too: they must not keep modifying the
                # child-owned report while the trusted parent checks it.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if report.is_symlink() or not report.is_file() or report.stat().st_size > 16 * 1024 * 1024:
            (logs / "result.json").write_text(
                json.dumps({"passed": False, "returncode": code, "reason": "invalid_report"})
            )
            return
        text = report.read_text()
        (logs / "results.xml").write_text(text)
        try:
            parsed = parser.parse_junit(text, returncode=code)
        except ValueError as exc:
            (logs / "result.json").write_text(
                json.dumps({"passed": False, "returncode": code, "reason": str(exc)})
            )
            return
        expected = set(contract["expected_passes"])
        passed = code == 0 and bool(expected) and parsed.passed == expected
        (logs / "result.json").write_text(
            json.dumps({"passed": passed, "returncode": code, "statuses": parsed.statuses})
        )
        reward.write_text("1\n" if passed else "0\n")


if __name__ == "__main__":
    main()
