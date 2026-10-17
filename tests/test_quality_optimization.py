"""Development isolation, balanced candidate scoring and safe search resumption."""

from __future__ import annotations

import json
import shutil

import pytest
from pydantic import ValidationError

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.benchmark.optimization import (
    Guidance,
    OptimizationOptions,
    choose_candidate,
    optimize,
    validate_guidance,
)
from repo2rlenv.quality.loop.artifacts import refresh_identity
from tests import test_quality_reviewer as fixtures
from tests.test_quality_benchmark import make_case

task = fixtures.task


def options():
    return OptimizationOptions(
        models=[
            {"provider": "openai", "model": "test-openai"},
            {"provider": "anthropic", "model": "test-sonnet"},
        ],
        development_families=["arithmetic"],
    )


def metrics(**changes):
    return dict(false_blocked=0, detected=1, decidable=2, execution_errors=0, **changes)


def test_selection_requires_no_regression_on_either_model():
    stock = [metrics(), metrics()]
    worse = [dict(metrics(), detected=2), dict(metrics(), false_blocked=1)]
    assert choose_candidate({"stock": stock, "bad": worse, "tie": stock}) == "stock"
    better = [dict(metrics(), detected=2), metrics()]
    assert choose_candidate({"stock": stock, "good": better}) == "good"


def test_search_requires_balanced_routes_and_unmodified_stock():
    data = options().model_dump()
    data["models"][1]["provider"] = "openai"
    with pytest.raises(ValidationError, match="one OpenAI"):
        OptimizationOptions(**data)
    data = options().model_dump()
    data["review"].update(profile="evidence-review-v2", review_guidance="changed")
    with pytest.raises(ValidationError, match="stock"):
        OptimizationOptions(**data)


def test_guidance_is_pinned_and_only_valid_for_the_evidence_profile():
    from repo2rlenv.quality.loop.models import LoopOptions
    from repo2rlenv.quality.reviewer.profile import identity

    with pytest.raises(ValidationError, match="guidance"):
        LoopOptions(review_guidance="Trace the active entrypoint.")
    stock = LoopOptions(profile="evidence-review-v2")
    guided = LoopOptions(
        profile="evidence-review-v2", review_guidance="Trace the active entrypoint."
    )
    assert identity(stock)["review_prompt_sha256"] != identity(guided)["review_prompt_sha256"]


def test_exact_benchmark_literals_are_rejected_before_evaluation():
    from types import SimpleNamespace

    case = SimpleNamespace(
        case_id="c-0123456789ab",
        defect=SimpleNamespace(
            location=SimpleNamespace(quote="test -f /workspace/.private-marker")
        ),
    )
    for text in ("Block c-0123456789ab", "Check `test  -f /workspace/.private-marker` explicitly"):
        with pytest.raises(ValueError, match="benchmark"):
            validate_guidance(text, [case])
    validate_guidance("Trace guards and who supplies their prerequisites.", [case])


def corpus(task, tmp_path, *, status="adjudicated", split="development"):
    audit = {
        "status": status,
        "reviewers": ["curator", "independent-auditor"],
        "rationale": "Test fixture audit",
        "evidence": [{"path": "instruction.md", "quote": "sum of two integers"}],
    }
    broken = tmp_path / "broken"
    shutil.copytree(task, broken)
    (broken / "tests/test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    refresh_identity(broken)
    cases = [
        make_case(task, audit=audit, split=split),
        make_case(
            broken,
            audit=audit,
            split=split,
            case_id="c-0123456789ac",
            defect={
                "category": "verifier",
                "location": {"path": "tests/test.sh", "quote": "echo 1"},
                "explanation": "Unconditional reward",
            },
        ),
    ]
    manifest = tmp_path / "manifest.json"
    save_record(manifest, {"cases": cases})
    return manifest


@pytest.mark.parametrize("status,split", [("provisional", "development"), ("adjudicated", "final")])
def test_no_paid_call_for_unaudited_or_heldout_cases(task, tmp_path, status, split):
    manifest = corpus(task, tmp_path, status=status, split=split)
    with pytest.raises(ValueError, match=r"Audit|development"):
        optimize(
            manifest,
            options(),
            tmp_path / "run",
            BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1"),
            benchmark=lambda *a, **k: pytest.fail("must not dispatch"),
        )


@pytest.mark.parametrize("failed_provider", [None, "openai", "copied-literal"])
def test_balanced_proposals_and_candidate_evaluations(task, tmp_path, failed_provider):
    manifest = corpus(task, tmp_path)
    proposed, evaluated = [], []

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def ask(self, schema, model, system, context, key):
            proposed.append(model.provider)
            assert all(
                c["split"] == "development" for c in json.loads(context)["development_labels"]
            )
            if model.provider == failed_provider:
                raise RuntimeError("provider unavailable")
            if model.provider == "openai" and failed_provider == "copied-literal":
                return Guidance(guidance="Reject the exact line echo 1", rationale="Memorized")
            return Guidance(
                guidance="Trace the executed path to every assertion.", rationale="Coverage"
            )

    def benchmark(path, config, directory, ledger, **kwargs):
        evaluated.append((directory.parent.name, config.review.review_model.provider))
        return {
            "summary": {
                "profiles": {
                    "evidence-review-v2": {
                        "provisional": {"attempts": 0},
                        "adjudicated": metrics(),
                    }
                }
            }
        }

    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1")
    result = optimize(
        manifest,
        options(),
        tmp_path / "run",
        ledger,
        benchmark=benchmark,
        model_factory=Model,
    )
    assert proposed == ["openai", "anthropic"]
    if failed_provider:
        assert result["state"] == "incomplete" and result["selected"] is None
        assert len(evaluated) == 2
    else:
        assert result["selected"] == "stock"
        assert result["promotion"] == "selection_and_final_required"
        assert evaluated == [
            (c, p) for c in ["stock", "openai", "anthropic"] for p in ["openai", "anthropic"]
        ]
    with pytest.raises(ValueError, match="resume"):
        optimize(
            manifest, options(), tmp_path / "run", ledger, benchmark=benchmark, model_factory=Model
        )
