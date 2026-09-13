from __future__ import annotations

import json

import pytest

from repo2rlenv.quality.loop.context import EvidenceContext
from repo2rlenv.quality.loop.models import ReadRequest


@pytest.mark.parametrize("count", [800, 2400])
def test_large_inventory_retains_every_addressable_file(tmp_path, count):
    task = tmp_path / "task"
    task.mkdir()
    (task / "instruction.md").write_text("Repair the package behavior.")
    for index in range(count):
        path = task / "environment/source/package" / f"module_{index:04}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"VALUE = {index}\n")
    context = EvidenceContext(task, [], limit=16000)
    payload = json.loads(context.payload())
    assert len(json.dumps(payload)) <= 86000
    requested = f"environment/source/package/module_{count - 1:04}.py"
    if count == 800:
        entries = {
            "/".join(filter(None, [prefix, name])): size
            for prefix, files in payload["inventory_by_directory"].items()
            for name, size in files.items()
        }
        assert entries == {item["path"]: item["bytes"] for item in context.inventory}
    else:
        catalogue = payload["inventory_document"]
        assert payload["inventory_files"] == len(context.inventory)
        query = f"module_{count - 1:04}.py"
        context.read_more([ReadRequest(path=catalogue, start_line=1, end_line=1, query=query)])
        assert requested in context.documents[catalogue + ":search=" + query]
    context.read_more([ReadRequest(path=requested, start_line=1, end_line=1, query=None)])
    assert context.documents[requested + ":L1-L1"] == f"VALUE = {count - 1}\n"


def test_rejected_large_read_does_not_poison_next_review(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "instruction.md").write_text("Repair the package behavior.")
    (task / "large.txt").write_text(("x" * 1000 + "\n") * 100)
    context = EvidenceContext(task, [], limit=16000)
    before = dict(context.documents)
    with pytest.raises(ValueError, match="context budget"):
        context.read_more([ReadRequest(path="large.txt", start_line=1, end_line=100, query=None)])
    assert context.documents == before
    context.read_more([ReadRequest(path="large.txt", start_line=1, end_line=1, query=None)])
    assert context.documents["large.txt:L1-L1"] == "x" * 1000 + "\n"
    context.payload()
