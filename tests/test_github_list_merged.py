import json
from datetime import date

import pytest

from repo2rlenv import github


def _row(number, merged_at):
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "state": "MERGED",
        "mergedAt": merged_at,
        "baseRefName": "main",
        "headRefOid": "h",
        "isDraft": False,
        "url": f"https://github.com/o/n/pull/{number}",
        "files": [],
    }


@pytest.fixture
def gh(monkeypatch):
    calls = []
    rows = [_row(1, "2026-07-02T00:00:00Z"), _row(2, "2026-07-10T00:00:00Z")]

    def fake(args, token=None):
        calls.append(args)
        return json.dumps(rows)

    monkeypatch.setattr(github, "_run_gh", fake)
    monkeypatch.setattr(github, "_fetch_base_sha", lambda *a, **k: "b" * 40)
    return calls


@pytest.mark.parametrize(
    "since,until,search",
    [
        (date(2026, 7, 1), date(2026, 7, 15), "merged:2026-07-01..2026-07-15"),
        (date(2026, 7, 1), None, "merged:>=2026-07-01"),
        (None, date(2026, 7, 15), "merged:<=2026-07-15"),
    ],
)
def test_date_window_is_searched_server_side(gh, since, until, search):
    prs = github.list_merged_prs("o", "n", limit=5, since=since, until=until)
    assert gh[0][gh[0].index("--search") + 1] == search
    assert [pr.number for pr in prs] == [2, 1]  # newest first, whatever the search order


def test_no_window_lists_newest_without_search(gh):
    github.list_merged_prs("o", "n", limit=5)
    assert "--search" not in gh[0]
