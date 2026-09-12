from __future__ import annotations

import json

import pytest

from repo2rlenv.cli import main


def test_campaign_initialization_never_resets_an_existing_budget(tmp_path, capsys):
    assert main(["campaign", "init", str(tmp_path), "--budget-usd", "7", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["remaining_usd"] == "7.000000"
    with pytest.raises(ValueError, match="implicit budget reset"):
        main(["campaign", "init", str(tmp_path), "--budget-usd", "9", "--json"])
    assert main(["campaign", "status", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["limit_usd"] == "7.000000"
