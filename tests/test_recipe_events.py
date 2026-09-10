from __future__ import annotations

import json

import pytest
from rich.console import Console

from repo2rlenv.campaigns.events import EventJournal, ProgressEvent
from repo2rlenv.ui.views.recipe import RecipeView, recipe_events


def test_json_and_journal_receive_the_same_events(tmp_path, capsys):
    journal = EventJournal(tmp_path / "events.jsonl")
    view = RecipeView("fixture", "[red]untrusted[/red]", 20, "modal")
    event = ProgressEvent(
        recipe="fixture",
        stage="verify",
        state="failed",
        message="[red]literal[/red]",
        metrics={"accepted": 0, "exported": 1},
    )
    with recipe_events(view, journal, json_output=True) as emit:
        emit(event)
    assert json.loads(capsys.readouterr().out) == journal.read()[0]
    terminal = Console(width=45, record=True)
    terminal.print(view.render())
    assert "[red]untrusted[/red]" in terminal.export_text()


def test_interrupted_tail_is_not_treated_as_a_completed_event(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl")
    event = ProgressEvent(recipe="fixture", stage="author", state="started")
    journal.emit(event)
    with journal.path.open("ab") as handle:
        handle.write(b'{"partial":')
    assert len(journal.read()) == 1
    with pytest.raises(ValueError, match="incomplete tail"):
        journal.emit(event)


def test_corrupted_complete_record_is_not_silently_skipped(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl")
    journal.path.write_bytes(b"{invalid}\n")
    with pytest.raises(json.JSONDecodeError):
        journal.read()
