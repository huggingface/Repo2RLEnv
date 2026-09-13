"""Standalone trusted entry point; copied into each separate verifier image.

No Repo2RLEnv installation is needed to grade an exported task. Submitted Python
runs as an unprivileged user; this parent owns the result parser and reward file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def child_environment(temporary: str) -> dict[str, str]:
    """Preserve prepared runtime assets without inheriting controller credentials."""
    return {
        **{
            key: os.environ[key]
            for key in (
                "LD_LIBRARY_PATH",
                "CUDA_VISIBLE_DEVICES",
                "NVIDIA_VISIBLE_DEVICES",
                "NVIDIA_DRIVER_CAPABILITIES",
                "NCCL_SOCKET_IFNAME",
                "GLOO_SOCKET_IFNAME",
                "HF_HOME",
                "HF_HUB_CACHE",
                "HF_HUB_OFFLINE",
                "TRANSFORMERS_OFFLINE",
                "HF_DATASETS_OFFLINE",
            )
            if key in os.environ
        },
        "PATH": str(Path(sys.executable).parent) + ":/usr/local/bin:/usr/bin:/bin",
        "HOME": temporary,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def validate_submission(workspace: Path, contract: dict) -> None:
    """Validate collected data before any learner-controlled Python is imported."""
    submitted = set(contract["submitted_files"])
    optional = submitted & set(contract.get("optional_files", []))
    paths = submitted - optional
    for relative in optional:
        path = workspace / relative
        if any(component.is_symlink() for component in (path, *path.parents)):
            raise ValueError("Submitted source contains a symlink")
        # Added standalone modules may be absent in the baseline. If supplied,
        # apply the same regular-file and size checks as every required module.
        if path.exists():
            paths.add(relative)
    roots = contract.get("submitted_roots", [])
    immutable = contract.get("immutable_assets", {})
    paths.update(immutable)
    for relative in roots:
        root = workspace / relative
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Submitted source root is missing or linked")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("Submitted source contains a symlink")
            if path.is_dir():
                continue
            name = path.relative_to(workspace).as_posix()
            if path.suffix != ".py" and name not in immutable:
                raise ValueError("Only Python source files may be added")
            paths.add(name)
    for relative in sorted(paths):
        path = workspace / relative
        for component in (path, *path.parents):
            if component.is_symlink():
                raise ValueError("Submitted source contains a symlink")
        if (
            not path.is_file()
            or not stat.S_ISREG(path.stat().st_mode)
            or path.stat().st_size > 8 * 1024 * 1024
        ):
            raise ValueError("Submitted source is not a bounded regular file")
        if (
            relative in immutable
            and hashlib.sha256(path.read_bytes()).hexdigest() != immutable[relative]
        ):
            raise ValueError("Non-Python source assets must remain unchanged")
        os.chown(path, 0, 0)
        path.chmod(0o644)


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
    try:
        validate_submission(Path("/workspace"), contract)
    except (ValueError, OSError) as exc:
        (logs / "result.json").write_text(json.dumps({"passed": False, "reason": str(exc)}))
        return
    with tempfile.TemporaryDirectory(prefix="r2e-grade-") as temporary:
        working = Path(temporary)
        os.chown(working, 1001, 1001)
        working.chmod(0o700)
        report = working / "results.xml"
        command = [
            sys.executable,
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
                env=child_environment(temporary),
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
