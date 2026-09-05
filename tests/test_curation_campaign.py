from __future__ import annotations

from types import SimpleNamespace

import pytest

from repo2rlenv.curation import campaign
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import CampaignConfig, TrialEvidence


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_error", [None, "Remote export failed"])
async def test_terminal_failure_preserves_review_or_execution_evidence(
    tmp_path, monkeypatch, execution_error
):
    class Sandbox:
        def __init__(self, timeout):
            self.sandbox = SimpleNamespace(object_id="mock")

        async def start(self):
            pass

        async def stop(self):
            pass

        async def prepare(self, source):
            pass

        async def export(self, task):
            task.mkdir(parents=True)

        async def shell(self, **kwargs):
            return ""

    async def noop(**kwargs):
        pass

    async def preflight(task, output, **kwargs):
        output.mkdir(parents=True)
        return [
            TrialEvidence(
                label=label,
                task_digest="digest",
                reward=reward,
                error=execution_error,
                path=str(output),
            )
            for label, reward in [("baseline", 0), ("oracle-0", 1)]
        ]

    async def trial(task, output, label, **kwargs):
        return TrialEvidence(
            label=label,
            task_digest="digest",
            reward=int(label.startswith("oracle")),
            path=str(output),
        )

    async def review(*args, **kwargs):
        return SimpleNamespace(
            score=72.5, adversary_assessment="attempted_hack", model_dump_json=lambda: "{}"
        )

    monkeypatch.setattr(campaign, "AuthorSandbox", Sandbox)
    monkeypatch.setattr(campaign, "run_agent", noop)
    monkeypatch.setattr(campaign, "preflight", preflight)
    monkeypatch.setattr(campaign, "trial", trial)
    monkeypatch.setattr(campaign, "review", review)
    monkeypatch.setattr(campaign, "digest_task", lambda _: "digest")
    monkeypatch.setattr(
        campaign,
        "finalize",
        lambda *args: SimpleNamespace(
            mutations=[],
            equivalents=[],
            source_paths=["src/pkg"],
        ),
    )
    monkeypatch.setattr(campaign, "acceptance", lambda *args: ["Insufficient coverage"])
    result = await campaign.curate_one(
        {"id": "test", "url": "https://example.test/pr"},
        tmp_path / "candidate",
        CampaignConfig(max_revisions=1, oracle_repeats=2),
        Budget(tmp_path / "ledger.json", 100),
    )
    if execution_error:
        assert result["status"] == "execution_failure"
        assert result["execution_errors"][0]["error"] == execution_error
        assert "score" not in result
    else:
        assert result["status"] == "rejected"
        assert result["score"] == 72.5
        assert result["task_digest"] == "digest"
        assert result["review_path"].endswith("/revision-0/review.json")
