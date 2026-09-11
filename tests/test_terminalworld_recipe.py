from __future__ import annotations

import json

from repo2rlenv.pipelines.recipes.terminalworld.capture import changes, filesystem_state
from repo2rlenv.pipelines.recipes.terminalworld.source import MetadataParser, load_recordings


def test_recording_screen_excludes_flagged_text_from_author_input(tmp_path):
    first = tmp_path / "123"
    first.mkdir()
    (first / "info.json").write_text(json.dumps({"title": "An input recording", "id": "123"}))
    (first / "recording.txt").write_text("download from 203.0.113.4 then run a task")
    record = load_recordings(tmp_path)[0]
    assert record["filter_flags"] == ["pii_public_ipv4"]
    assert record["transcript"] == ""
    assert len(record["transcript_sha256"]) == 64


def test_metadata_reads_description_and_ignores_unrelated_secrets():
    parser = MetadataParser()
    parser.feed(
        '<meta name="csrf-token" content="ignore-me"><meta property="og:title" content="Task title"><div class="description">Workflow <div>details</div> follow.</div><div>unrelated</div>'
    )
    assert parser.metadata == {"title": "Task title"}
    assert "".join(parser.description) == "Workflow details follow."


def test_snapshot_detects_new_modified_and_deleted_files(tmp_path):
    edited = tmp_path / "edited.txt"
    edited.write_text("before")
    removed = tmp_path / "removed.txt"
    removed.write_text("remove")
    before = filesystem_state([tmp_path])
    edited.write_text("after!")
    removed.unlink()
    created = tmp_path / "created.txt"
    created.write_text("created")
    delta = changes(before, filesystem_state([tmp_path]))
    assert delta == {
        "created": [str(created)],
        "modified": [str(edited)],
        "deleted": [str(removed)],
    }
