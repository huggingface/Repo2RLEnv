"""Requested source excerpts fit without shortening previously supplied evidence."""

from __future__ import annotations

import pytest

from repo2rlenv.quality.loop.context import EvidenceContext, _search_excerpts
from repo2rlenv.quality.loop.models import ReadRequest


def packed_context(tmp_path, source, *, remaining):
    (tmp_path / "instruction.md").write_text("Preserve the documented behavior.\n")
    (tmp_path / "source.txt").write_text(source)
    context = EvidenceContext(tmp_path, [], limit=100000)
    used = sum(map(len, context.documents.values()))
    context.documents["evidence/retained.txt"] = "R" * (context.limit - used - remaining)
    return context


def search():
    return ReadRequest(path="source.txt", query="MARKER", start_line=1, end_line=1)


def test_overlapping_search_windows_do_not_duplicate_source_to_exhaust_budget(tmp_path):
    lines = [f"# line {index:03}: preserve this exact source text\n" for index in range(100)]
    for index in (7, 13, 24, 25, 29, 32, 36, 46):
        lines[index] = f"def target_{index}():  # MARKER\n"
    context = packed_context(tmp_path, "".join(lines), remaining=5092)
    before = dict(context.documents)
    context.read_more([search()])
    excerpt = context.documents["source.txt:search=MARKER"]
    assert len(excerpt) <= 5092
    assert excerpt.count("[Lines ") < 8
    assert excerpt.count("def target_") == 8
    assert "Search limited to the first 8 matching windows" in excerpt
    assert all(context.documents[key] == value for key, value in before.items())
    assert sum(map(len, context.documents.values())) <= context.limit


def test_budget_limited_search_keeps_complete_windows_and_declares_omissions(tmp_path):
    source = ("MARKER " + "x" * 5000) * 12
    context = packed_context(tmp_path, source, remaining=5092)
    before = dict(context.documents)
    context.read_more([search()])
    excerpt = context.documents["source.txt:search=MARKER"]
    assert excerpt.count("MARKER") == 1
    assert "7 additional search window(s) omitted" in excerpt
    assert "Line 1, columns 1-3000" in excerpt
    assert source[:3000] in excerpt
    assert all(context.documents[key] == value for key, value in before.items())
    assert sum(map(len, context.documents.values())) <= context.limit


def test_repeated_read_never_shortens_an_existing_citable_excerpt(tmp_path):
    context = packed_context(tmp_path, ("MARKER " + "x" * 5000) * 12, remaining=26000)
    context.read_more([search()])
    context.documents["evidence/later.txt"] = "L" * (
        context.limit - sum(map(len, context.documents.values()))
    )
    before = dict(context.documents)
    context.read_more([search()])
    assert context.documents == before


def test_unfittable_search_reports_path_and_budget_without_mutation(tmp_path):
    context = packed_context(tmp_path, "MARKER " + "x" * 5000, remaining=100)
    before = dict(context.documents)
    with pytest.raises(
        ValueError, match=r"source\.txt: Additional reads exceeded context budget"
    ) as error:
        context.read_more([search()])
    assert "100 characters remain" in str(error.value)
    assert "short explicit line range" in str(error.value)
    assert context.documents == before


def test_requested_full_range_is_not_silently_clipped(tmp_path):
    context = packed_context(tmp_path, ("x" * 100 + "\n") * 10, remaining=200)
    before = dict(context.documents)
    with pytest.raises(ValueError) as error:
        context.read_more([ReadRequest(path="source.txt", query=None, start_line=1, end_line=10)])
    assert "source.txt:L1-L10 requires 1010 characters but 200 remain" in str(error.value)
    assert context.documents == before


def test_failed_multi_read_is_atomic_and_describes_the_unfittable_request(tmp_path):
    context = packed_context(tmp_path, ("x" * 100 + "\n") * 10, remaining=200)
    before = dict(context.documents)
    with pytest.raises(ValueError, match=r"source\.txt:L2-L4"):
        context.read_more(
            [
                ReadRequest(path="source.txt", query=None, start_line=1, end_line=1),
                ReadRequest(path="source.txt", query=None, start_line=2, end_line=4),
            ]
        )
    assert context.documents == before


def test_bounded_search_preserves_no_match_result():
    assert (
        _search_excerpts(["unrelated text"], "MARKER", maximum=100) == "[No literal matches found]"
    )
