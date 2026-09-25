from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from repo2rlenv.execution.artifacts import unpack_evidence
from repo2rlenv.execution.harbor import read_trial


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("job/link", tarfile.SYMTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("job/pipe", tarfile.FIFOTYPE),
    ],
)
def test_remote_evidence_rejects_unsafe_archives_before_publishing(tmp_path, name, kind):
    archive = tmp_path / "evidence.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "/etc/passwd"
        stream.addfile(member)
    with pytest.raises(ValueError, match="unsafe"):
        unpack_evidence(archive, tmp_path / "output", root_name="job")
    assert not (tmp_path / "output/job").exists()


def test_archive_size_limit_is_checked_before_extraction(tmp_path):
    archive = tmp_path / "evidence.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("job/binary")
        member.size = 8
        stream.addfile(member, io.BytesIO(b"\0" * 8))
    with pytest.raises(ValueError, match="limits"):
        unpack_evidence(archive, tmp_path / "output", root_name="job", max_bytes=4)
    assert not (tmp_path / "output/job").exists()


def test_trial_exception_cannot_be_reported_as_valid_contrast(tmp_path):
    (tmp_path / "trial").mkdir()
    path = tmp_path / "trial/result.json"
    path.write_text(
        json.dumps(
            {
                "exception_info": {"exception_type": "RuntimeError"},
                "verifier_result": {"rewards": {"reward": 0}},
            }
        )
    )
    assert not read_trial(tmp_path).completed
    path.write_text(
        json.dumps({"exception_info": None, "verifier_result": {"rewards": {"reward": 0}}})
    )
    assert read_trial(tmp_path).completed
    (tmp_path / "second-trial").mkdir()
    (tmp_path / "second-trial/result.json").write_text(path.read_text())
    with pytest.raises(ValueError, match="exactly one"):
        read_trial(tmp_path)


def test_parallel_runtime_installs_share_one_complete_environment(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Event

    from repo2rlenv.execution.runtime_install import install

    content = b"owned test wheel"
    root = tmp_path / hashlib.sha256(content).hexdigest()
    uploads = [root / f"upload-{i}/repo2rlenv-0.9.1-py3-none-any.whl" for i in range(2)]
    for path in uploads:
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    calls = []
    installing, release = Event(), Event()

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "venv":
            python = Path(argv[-1]) / "bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
        else:
            installing.set()
            assert release.wait(5)

    monkeypatch.setattr("repo2rlenv.execution.runtime_install.subprocess.run", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(install, root, uploads[0])
        assert installing.wait(5)
        second = pool.submit(install, root, uploads[1])
        assert not (root / "installed").exists()
        release.set()
        first.result()
        second.result()
    assert [argv[1] for argv in calls] == ["venv", "pip"]
    assert (root / "installed").read_text().strip() == root.name
    assert not any(path.parent.exists() for path in uploads)


def test_runtime_hash_mismatch_never_runs_installation(tmp_path, monkeypatch):
    from repo2rlenv.execution.runtime_install import install

    root = tmp_path / ("a" * 64)
    uploaded = root / "upload-1/runtime.whl"
    uploaded.parent.mkdir(parents=True)
    uploaded.write_bytes(b"wrong content")
    monkeypatch.setattr(
        "repo2rlenv.execution.runtime_install.subprocess.run",
        lambda *a, **k: pytest.fail("Corrupt wheel must not execute"),
    )
    with pytest.raises(ValueError, match="content address"):
        install(root, uploaded)
    assert not (root / "installed").exists()
