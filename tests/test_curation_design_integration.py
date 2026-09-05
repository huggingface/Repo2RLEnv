from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from repo2rlenv.curation import campaign, design
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import CampaignConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("submit", [True, False])
async def test_planning_is_a_required_predecessor_to_implementation(tmp_path, monkeypatch, submit):
    events = []
    written = {}
    root = tmp_path / "candidate"

    class Sandbox:
        def __init__(self, timeout):
            self.sandbox = SimpleNamespace(object_id="planning-integration")

        async def start(self):
            events.append("start")

        async def prepare(self, source):
            events.append("prepare")

        async def stop(self):
            events.append("stop")

        async def shell(self, command, timeout_sec=120):
            return json.dumps({"exit_code": 0, "stdout": "", "stderr": ""})

        async def write(self, path, text):
            assert (root / "design.json").is_file()
            written[path] = text
            events.append("write_plan")

        async def export(self, task):
            task.mkdir(parents=True)
            (task / "authoring-context.json").write_text('{"screening_observations": []}')
            events.append("export")

    async def planner(**kwargs):
        events.append("plan")
        assert not (root / "submitted-drafts.json").exists()
        if submit:
            await kwargs["handlers"]["submit_design"](
                task_request="Implement the public sum and stable ordering behavior for the package.",
                verification_plan={
                    "behaviors": [
                        {
                            "requirement": name,
                            "expected_result": "Compute expected values independently from fixed varied inputs.",
                            "tests": ["test_" + name],
                            "mutations": ["wrong_" + name],
                            "equivalents": ["alternative"],
                        }
                        for name in ["sum", "order"]
                    ],
                    "offline_dependencies": "Use pinned packages with locally constructed fixtures.",
                    "artifact_boundary": "The whole editable source package includes helper modules.",
                },
            )
        return {}

    async def author(**kwargs):
        assert submit
        assert "/output/task/verification-plan.json" in written
        assert len(json.loads(written["/output/task/verification-plan.json"])["behaviors"]) == 2
        assert not (root / "submitted-drafts.json").exists()
        events.append("implement")
        assert "Structural" in await kwargs["handlers"]["validate_candidate"]()
        raise campaign.CandidateDeferred("Mock ends before environment execution")

    def check_context(task, source):
        context = json.loads((task / "authoring-context.json").read_text())
        assert context["screening_observations"] == ["Preserve both changed call sites."]
        assert context["initial_design"]["task_request"].startswith("Implement the public sum")
        raise ValueError("Mock stops before finalizing an environment")

    monkeypatch.setattr(campaign, "AuthorSandbox", Sandbox)
    monkeypatch.setattr(design, "run_agent", planner)
    monkeypatch.setattr(campaign, "run_agent", author)
    monkeypatch.setattr(campaign, "finalize", check_context)
    expected = campaign.CandidateDeferred if submit else design.DesignNotSubmitted
    with pytest.raises(expected):
        await campaign.curate_one(
            {
                "id": "source",
                "url": "https://github.com/a/b/pull/1",
                "screening_observations": ["Preserve both changed call sites."],
            },
            root,
            CampaignConfig(require_verification_plan=True, max_candidate_drafts=2),
            Budget(tmp_path / "budget.json", 8),
        )
    assert events == (
        ["start", "prepare", "plan", "write_plan", "implement", "export", "stop"]
        if submit
        else ["start", "prepare", "plan", "stop"]
    )
