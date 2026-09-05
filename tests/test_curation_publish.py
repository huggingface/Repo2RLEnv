from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.campaign import ADMISSION_VERSION
from repo2rlenv.curation.publish import evidence_snapshot, publish_evidence


def write(path: Path, value: dict | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)


@pytest.fixture
def api(monkeypatch):
    import huggingface_hub

    class FakeApi:
        private = True

        def __init__(self):
            self.created = []
            self.uploads = []

        def create_bucket(self, bucket, *, private, exist_ok):
            self.created.append((bucket, private, exist_ok))

        def bucket_info(self, bucket):
            return SimpleNamespace(private=self.private)

        def batch_bucket_files(self, bucket, *, add):
            for data, name in add:
                self.uploads.append(
                    (name, data if isinstance(data, bytes) else Path(data).read_bytes())
                )

    fake = FakeApi()
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: fake)
    return fake


def populate(root: Path, *, comparison: bool):
    rows = []
    for runtime in ["langgraph", "pi", "opencode"] if comparison else [None]:
        task = root / "tasks"
        if runtime:
            task /= runtime
        task /= "example-project-1"
        write(task / "contract.json", {"task": "example"})
        write(task / "solution/patch.diff", "--- a/source.py\n+++ b/source.py\n")
        row = {
            "id": "example-project-1",
            "status": "accepted",
            "task_digest": digest_task(task),
            "admission_version": ADMISSION_VERSION,
        }
        if runtime:
            row["runtime"] = runtime
        rows.append(row)
    name = "comparison.json" if comparison else "manifest.json"
    manifest = {"rows" if comparison else "accepted": rows, "human_review": "pending"}
    write(root / name, manifest)
    return name, manifest


@pytest.mark.parametrize("comparison", [False, True])
def test_private_publication_supports_campaign_and_comparison_manifests(tmp_path, api, comparison):
    name, _ = populate(tmp_path, comparison=comparison)
    evidence = tmp_path / "candidates/run/revision-0"
    text = "actual_changed_source = 1\n"
    key = "trials/solver-0-0/artifacts/workspace/src/pkg.py"
    write(
        evidence / "review-submissions.json",
        {
            "schema_version": 1,
            "texts": {key: text},
            "sha256": {key: hashlib.sha256(text.encode()).hexdigest()},
        },
    )
    write(
        evidence / "review-evidence.json", {"submission_text_snapshot": "review-submissions.json"}
    )
    write(evidence / "trials/solver-0-0/artifacts/workspace/src/pkg.py", "raw export excluded")
    url = publish_evidence(tmp_path, "owner/private-evidence")
    assert api.created == [("owner/private-evidence", True, True)]
    assert url.startswith("https://huggingface.co/buckets/owner/private-evidence/tree/")
    uploaded = dict(api.uploads)
    prefix = url.rsplit("/", 1)[1]
    assert f"{prefix}/{name}" in uploaded
    snapshot = json.loads(uploaded[f"{prefix}/candidates/run/revision-0/review-submissions.json"])
    assert snapshot["texts"][key] == text
    assert not any("/artifacts/" in path for path in uploaded)
    checksums = json.loads(uploaded[f"{prefix}/checksums.json"])
    for path, checksum in checksums.items():
        assert hashlib.sha256(uploaded[f"{prefix}/{path}"]).hexdigest() == checksum
    for runtime in ["langgraph", "pi", "opencode"] if comparison else [None]:
        relative = f"tasks/{runtime}/example-project-1" if runtime else "tasks/example-project-1"
        assert f"{prefix}/{relative}/solution/patch.diff" in uploaded


@pytest.mark.parametrize("comparison", [False, True])
@pytest.mark.parametrize("damage", ["digest", "missing", "admission"])
def test_publication_refuses_changed_missing_or_stale_accepted_tasks(
    tmp_path, api, comparison, damage
):
    name, manifest = populate(tmp_path, comparison=comparison)
    row = manifest["rows" if comparison else "accepted"][0]
    task = tmp_path / "tasks"
    if comparison:
        task /= row["runtime"]
    task /= row["id"]
    if damage == "digest":
        (task / "contract.json").write_text("changed")
    elif damage == "missing":
        shutil.rmtree(task)
    else:
        row["admission_version"] -= 1
        write(tmp_path / name, manifest)
    with pytest.raises(ValueError, match="Accepted task"):
        publish_evidence(tmp_path, "owner/private-evidence")
    assert not api.created
    assert not api.uploads


def test_public_existing_bucket_never_receives_evidence(tmp_path, api):
    populate(tmp_path, comparison=True)
    api.private = False
    with pytest.raises(ValueError, match="private bucket"):
        publish_evidence(tmp_path, "owner/public-bucket")
    assert not api.uploads


def test_snapshot_prunes_runtime_credentials_and_caches_but_preserves_traces(tmp_path):
    write(tmp_path / "comparison.json", {"rows": []})
    runtime = tmp_path / "candidates/opencode/example/run/author-0-runtime"
    for name in (
        "opencode-events.jsonl",
        "opencode-messages.json",
        "runtime-result.json",
        "runtime-stderr.log",
        "native-session.jsonl",
    ):
        write(runtime / name, "retained evidence")
    write(runtime / "runner-config.json", "local bridge credential")
    write(runtime / "auth.json", "credential")
    write(runtime / "opencode-session/server.log", "server evidence")
    write(runtime / "opencode-session/bridge.json", "local bridge credential")
    write(runtime / "opencode-session/config/opencode/opencode.json", "provider configuration")
    write(runtime / "home/.config/settings.json", "ambient settings")
    linked = runtime / "opencode-session/config/opencode/node_modules"
    linked.symlink_to(tmp_path.parent, target_is_directory=True)
    write(tmp_path / "review-submissions.json", {"texts": {"source": "inspected source"}})
    with evidence_snapshot(tmp_path) as snapshot:
        copied = snapshot / runtime.relative_to(tmp_path)
        assert (copied / "opencode-events.jsonl").read_text() == "retained evidence"
        assert (copied / "native-session.jsonl").exists()
        assert (copied / "runtime-stderr.log").exists()
        assert (copied / "opencode-session/server.log").read_text() == "server evidence"
        assert not (copied / "runner-config.json").exists()
        assert not (copied / "auth.json").exists()
        assert not (copied / "opencode-session/bridge.json").exists()
        assert not (copied / "opencode-session/config").exists()
        assert not (copied / "home").exists()
        write(tmp_path / "review-submissions.json", {"texts": {"source": "changed later"}})
        assert (
            json.loads((snapshot / "review-submissions.json").read_text())["texts"]["source"]
            == "inspected source"
        )


def test_comparison_rejects_invalid_runtime_and_ambiguous_manifests(tmp_path, api):
    name, manifest = populate(tmp_path, comparison=True)
    manifest["rows"][0]["runtime"] = "../outside"
    write(tmp_path / name, manifest)
    with pytest.raises(ValueError, match="comparison runtime"):
        publish_evidence(tmp_path, "owner/private-evidence")
    write(tmp_path / "manifest.json", {"accepted": []})
    with pytest.raises(ValueError, match="exactly one"):
        publish_evidence(tmp_path, "owner/private-evidence")
    assert not api.created
