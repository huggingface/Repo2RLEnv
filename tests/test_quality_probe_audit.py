from pathlib import Path

import pytest

from repo2rlenv.quality.loop.probe_audit import changed_files, submission_files


def test_probe_requires_a_change_to_collected_source(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    module = source / "answer.py"
    module.write_text("raise RuntimeError('must never import this file')\n")
    contract = {"submitted_roots": ["src"], "submitted_files": ["src/answer.py"]}
    before = submission_files(tmp_path, contract)
    (tmp_path / "extra-check.txt").write_text("passed")
    with pytest.raises(ValueError, match="unchanged"):
        changed_files(before, submission_files(tmp_path, contract))
    module.write_text("def answer(): return 42\n")
    assert set(changed_files(before, submission_files(tmp_path, contract))) == {"src/answer.py"}


def test_probe_tracks_new_and_removed_modules_but_not_immutable_assets(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    module = source / "answer.py"
    module.write_text("value = 1\n")
    asset = source / "vocab.json"
    asset.write_text("{}")
    contract = {
        "submitted_roots": ["src"],
        "submitted_files": ["src/answer.py", "src/vocab.json"],
        "immutable_assets": {"src/vocab.json": "protected"},
    }
    before = submission_files(tmp_path, contract)
    asset.write_text('{"changed": true}')
    with pytest.raises(ValueError, match="unchanged"):
        changed_files(before, submission_files(tmp_path, contract))
    module.unlink()
    (source / "helper.py").write_text("value = 1\n")
    changes = changed_files(before, submission_files(tmp_path, contract))
    assert set(changes) == {"src/answer.py", "src/helper.py"}
    assert changes["src/answer.py"]["after"] is None
    assert changes["src/helper.py"]["before"] is None


@pytest.mark.parametrize("name", ["../answer.py", "/answer.py", "src/../answer.py", "src\\answer.py"])
def test_probe_rejects_noncanonical_paths(tmp_path, name):
    with pytest.raises(ValueError, match="canonical"):
        submission_files(tmp_path, {"submitted_files": [name]})


def test_probe_does_not_follow_submission_links(tmp_path):
    (tmp_path / "source.py").symlink_to(Path(__file__))
    with pytest.raises(ValueError, match="symlink"):
        submission_files(tmp_path, {"submitted_files": ["source.py"]})
