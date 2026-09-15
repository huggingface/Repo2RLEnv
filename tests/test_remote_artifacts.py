from __future__ import annotations

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
