from __future__ import annotations

from types import SimpleNamespace

import pytest

from repo2rlenv.curation import harbor_agent
from repo2rlenv.curation.agent import IncompleteModelResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["solve", "adversary"])
async def test_incomplete_harbor_agent_preserves_cost_and_still_fails(tmp_path, monkeypatch, mode):
    state = {"messages": [{"role": "assistant", "content": None}], "turns": 2, "cost": 0.43}

    async def incomplete(**kwargs):
        raise IncompleteModelResponse("Output token limit reached", state)

    monkeypatch.setattr(harbor_agent, "run_agent", incomplete)
    agent = harbor_agent.OfflineAgent(
        logs_dir=tmp_path,
        model_name="anthropic/claude-sonnet-5",
        budget_path=str(tmp_path / "budget.json"),
        budget_limit=10,
        mode=mode,
    )
    context = SimpleNamespace(cost_usd=None, metadata=None)
    with pytest.raises(IncompleteModelResponse, match="Output token limit"):
        await agent.run("Task", None, context)
    assert context.cost_usd == 0.43
    assert context.metadata == {"turns": 2, "mode": mode}
