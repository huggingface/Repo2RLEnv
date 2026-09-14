"""Real Git fixture checks; no target imports, tests or images are executed."""

import hashlib
import json
import subprocess

import pytest

from repo2rlenv.tasksmith.worker import reverse_source


def blob(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def setup(tmp_path, files):
    checkout = tmp_path / "git"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=checkout, check=True)
    for name, (before, _) in files.items():
        if before is not None:
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(before)
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    for name, (_, after) in files.items():
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(after)
    # Intent-to-add includes newly introduced modules in the frozen source diff.
    subprocess.run(["git", "add", "-N", "."], cwd=checkout, check=True)
    patch = (
        subprocess.run(
            ["git", "diff", "--binary", "--full-index"],
            cwd=checkout,
            capture_output=True,
            check=True,
        )
        .stdout.replace(b"\r\n", b"\n")
        .decode()
    )
    output = tmp_path / "output"
    output.mkdir()
    source = {
        "source_diff": patch,
        "source_files": list(files),
        "changed_files": [
            {"filename": name, "status": "added" if before is None else "modified"}
            for name, (before, _) in files.items()
        ],
    }
    return checkout, output, source


@pytest.mark.parametrize("last_line", [b"tail\r\n", b"tail"])
def test_crlf_reverse_preserves_exact_pinned_bytes_and_untouched_files(tmp_path, last_line):
    files = {
        "lib/windows.py": (b"before\r\n" + last_line, b"after\r\n" + last_line),
        "lib/unix.py": (b"old\n", b"new\n"),
        "lib/binary.py": (b"unchanged\0data\r\n", b"unchanged\0data\r\n"),
    }
    checkout, output, source = setup(tmp_path, files)
    defective, removed = reverse_source(source, checkout, output)
    assert not removed
    for name, (before, after) in files.items():
        assert (defective / name).read_bytes() == before
        assert (checkout / name).read_bytes() == after
    assert (output / "source.diff").read_bytes() == source["source_diff"].encode()
    receipt = json.loads((output / "source-crlf-fallback.json").read_text())
    assert receipt["files"] == [
        {
            "path": "lib/windows.py",
            "preimage": blob(files["lib/windows.py"][0]),
            "postimage": blob(files["lib/windows.py"][1]),
        }
    ]


def test_added_crlf_file_is_removed_without_changing_other_sources(tmp_path):
    files = {"lib/added.py": (None, b"new\r\nmodule\r\n"), "lib/base.py": (b"same\n", b"same\n")}
    checkout, output, source = setup(tmp_path, files)
    defective, removed = reverse_source(source, checkout, output)
    assert removed == ("lib/added.py",)
    assert not (defective / "lib/added.py").exists()
    assert (checkout / "lib/added.py").read_bytes() == files["lib/added.py"][1]


def test_lf_patch_uses_normal_path_without_recovery_artifacts(tmp_path):
    checkout, output, source = setup(tmp_path, {"source.py": (b"old\n", b"new\n")})
    defective, _ = reverse_source(source, checkout, output)
    assert (defective / "source.py").read_bytes() == b"old\n"
    assert not (output / "source-crlf-fallback.json").exists()


@pytest.mark.parametrize(
    "change", ["content", "missing_blob", "wrong_preimage", "line_ending_change"]
)
def test_fallback_never_masks_a_true_source_or_identity_mismatch(tmp_path, change):
    before = b"old\n" if change == "line_ending_change" else b"old\r\n"
    checkout, output, source = setup(tmp_path, {"source.py": (before, b"new\r\n")})
    if change == "content":
        (checkout / "source.py").write_bytes(b"different\r\n")
    elif change == "missing_blob":
        source["source_diff"] = "\n".join(
            line for line in source["source_diff"].split("\n") if not line.startswith("index ")
        )
    elif change == "wrong_preimage":
        source["source_diff"] = source["source_diff"].replace(blob(before), "f" * 40)
    with pytest.raises(ValueError, match=r"frozen (?:postimage|preimage) blob"):
        reverse_source(source, checkout, output)
    assert (output / "defective-source/source.py").read_bytes() == (
        checkout / "source.py"
    ).read_bytes()
    assert not (output / "source-crlf-fallback.json").exists()


def test_changed_unterminated_line_preserves_absent_final_newline(tmp_path):
    before, after = b"prefix\r\nbefore", b"prefix\r\nafter"
    checkout, output, source = setup(tmp_path, {"source.py": (before, after)})
    defective, _ = reverse_source(source, checkout, output)
    assert (defective / "source.py").read_bytes() == before
    assert (checkout / "source.py").read_bytes() == after


def test_failed_fallback_does_not_publish_other_successful_patch_hunks(tmp_path):
    files = {"a.py": (b"old\n", b"new\n"), "z.py": (b"old\r\n", b"new\r\n")}
    checkout, output, source = setup(tmp_path, files)
    source["source_diff"] = "+invented\n".join(source["source_diff"].rsplit("+new\n", 1))
    with pytest.raises(ValueError, match="CRLF patch still does not apply exactly"):
        reverse_source(source, checkout, output)
    for name, (_, after) in files.items():
        assert (output / "defective-source" / name).read_bytes() == after
        assert (checkout / name).read_bytes() == after
    assert not (output / "source-crlf-fallback.json").exists()


@pytest.mark.parametrize("suffix", [b"mixed\n", b"binary\0\r\n", b"invalid\xff\r\n"])
def test_mixed_ending_or_binary_content_is_not_normalized(tmp_path, suffix):
    # A textual unified diff can describe a file whose untouched bytes contain
    # binary/invalid content; the fallback must still refuse to normalize it.
    checkout, output, source = setup(tmp_path, {"source.py": (b"old\r\n", b"new\r\n")})
    (checkout / "source.py").write_bytes(b"new\r\n" + suffix)
    with pytest.raises(ValueError, match="Command git failed"):
        reverse_source(source, checkout, output)
    assert (output / "defective-source/source.py").read_bytes() == b"new\r\n" + suffix
    assert not (output / "source-crlf-fallback.json").exists()
