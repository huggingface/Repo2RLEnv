"""Privacy, repeat independence, split discipline and benchmark denominators."""

from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.benchmark.models import BenchmarkOptions, Manifest, inventory, load_manifest
from repo2rlenv.quality.benchmark.runner import run_benchmark
from repo2rlenv.quality.benchmark.scoring import localized, score_case, summarize
from repo2rlenv.quality.loop.artifacts import task_identity
from repo2rlenv.quality.loop.models import LoopOptions, LoopResult
from tests import test_quality_reviewer as fixtures
from tests.test_quality_reviewer import decision

task = fixtures.task


def make_case(task, **updates):
    result = dict(
        case_id="c-0123456789ab",
        family="arithmetic",
        lineage=["repo/pr/1"],
        split="development",
        task=str(task),
        bundle_hash=task_identity(task),
        files=inventory(task),
        defect=None,
        audit=dict(
            status="provisional",
            reviewers=["curator"],
            rationale="Provisional source inspection",
            evidence=[dict(path="instruction.md", quote="sum of two integers")],
        ),
    )
    result.update(updates)
    return result


def test_shared_lineage_cannot_cross_splits(task):
    a = make_case(task)
    b = dict(a, case_id="c-0123456789ac", split="final")
    with pytest.raises(ValidationError, match="crosses"):
        Manifest(cases=[a, b])
    b.update(family="renamed")
    with pytest.raises(ValidationError, match="Shared lineage"):
        Manifest(cases=[a, b])


def test_adjudicated_requires_multiple_named_reviewers(task):
    data = make_case(task)
    data["audit"]["status"] = "adjudicated"
    with pytest.raises(ValidationError, match="two recorded"):
        Manifest(cases=[data])


def test_changed_case_and_truth_sidecar_rejected(task, tmp_path):
    path = tmp_path / "manifest.json"
    save_record(path, Manifest(cases=[make_case(task)]).model_dump(mode="json"))
    assert load_manifest(path)
    (task / "instruction.md").write_text("Changed")
    with pytest.raises(ValueError, match=r"changed|hash"):
        load_manifest(path)
    (task / "labels.json").write_text("{}")
    with pytest.raises(ValueError, match="Non-Harbor"):
        inventory(task)


class FakeLoop:
    calls: ClassVar[list] = []

    def __init__(self, options, output, ledger):
        self.options, self.output = options, output

    def run(self, task, *, resume=False):
        assert Path(task).name == "c-0123456789ab"
        assert "defect" not in str(task)
        self.calls.append((self.options.profile, self.output, task))
        return LoopResult(
            schema_version="2" if self.options.profile.endswith("v2") else "1",
            profile=self.options.profile,
            status="reviewed",
            source_hash=task_identity(task),
            bundle_hash=task_identity(task),
            task_path=str(task),
            repairs=0,
            review=decision().legacy(),
            trials=[],
            reasons=[],
            accounted_usd="0",
            reserved_usd="0",
        )


def test_independent_paired_repeats_and_blinded_paths(task, tmp_path):
    manifest = tmp_path / "private-manifest.json"
    save_record(manifest, Manifest(cases=[make_case(task)]).model_dump(mode="json"))
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1")
    FakeLoop.calls = []
    options = BenchmarkOptions()
    report = run_benchmark(
        manifest, options, tmp_path / "run", ledger, split="development", loop_factory=FakeLoop
    )
    assert len(FakeLoop.calls) == 4
    assert len({str(c[1]) for c in FakeLoop.calls}) == 4
    assert report["summary"]["profiles"]["evidence-review-v2"]["provisional"]["attempts"] == 2
    assert report["summary"]["profiles"]["evidence-review-v2"]["adjudicated"]["attempts"] == 0
    with pytest.raises(ValueError, match="identical"):
        run_benchmark(
            manifest, options, tmp_path / "run", ledger, split="development", loop_factory=FakeLoop
        )
    with pytest.raises(ValueError, match="identical"):
        run_benchmark(
            manifest,
            options.model_copy(update={"repeats": 3}),
            tmp_path / "run",
            ledger,
            split="development",
            resume=True,
            loop_factory=FakeLoop,
        )


def test_final_requires_fixed_selection(task, tmp_path):
    manifest = tmp_path / "manifest.json"
    save_record(manifest, Manifest(cases=[make_case(task, split="final")]).model_dump(mode="json"))
    with pytest.raises(ValueError, match="selection-report"):
        run_benchmark(
            manifest,
            BenchmarkOptions(),
            tmp_path / "run",
            BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1"),
            split="final",
        )


def test_missing_and_wrong_location_are_not_detections(task):
    defect = dict(
        category="verifier",
        location=dict(path="tests/test.sh", quote="Verify the sum"),
        explanation="No assertion",
    )
    case = Manifest(cases=[make_case(task, defect=defect)]).cases[0]
    issue = decision(fail=True).issues[0].model_dump()
    assert not localized(issue, case.defect)
    issue["evidence"] = [defect["location"]]
    assert localized(issue, case.defect)
    rows = [
        dict(score_case(case, None), profile=p, repeat=0)
        for p in ("practical-generation-v1", "evidence-review-v2")
    ]
    report = summarize(rows)
    data = report["profiles"]["evidence-review-v2"]["provisional"]
    assert data["defective_attempts"] == 1 and data["detection_rate"] == 0
    assert data["decision_coverage"] == 0 and data["execution_errors"] == 1
    assert report["finding_precision"] is None


def test_read_only_budget_is_enforced():
    with pytest.raises(ValidationError, match="no repair"):
        BenchmarkOptions(review=LoopOptions(repair=True))


def test_cli_registers_all_loop_options():
    import argparse

    from repo2rlenv.quality.loop.cli import add_quality_parser

    parser = argparse.ArgumentParser()
    add_quality_parser(parser.add_subparsers())
    args = parser.parse_args(["quality", "run", "task", "--campaign", "campaign", "--out", "out"])
    options = LoopOptions(**{key: getattr(args, key) for key in LoopOptions.model_fields})
    assert options.profile == "practical-generation-v1"
    assert (
        parser.parse_args(["quality", "benchmark", "validate", "x.json"]).benchmark_action
        == "validate"
    )


def test_grounded_block_is_decidable_even_with_other_unknowns(task):
    from types import SimpleNamespace

    review = decision(fail=True).legacy()
    review.task.status = "unknown"
    case = Manifest(cases=[make_case(task)]).cases[0]
    row = score_case(case, SimpleNamespace(review=review, status="needs_repair"))
    assert row["decidable"]
    review.issues = []
    assert not score_case(case, SimpleNamespace(review=review, status="needs_evidence"))[
        "decidable"
    ]


def test_selection_requires_audited_labels(task, tmp_path):
    manifest = tmp_path / "manifest.json"
    save_record(
        manifest, Manifest(cases=[make_case(task, split="selection")]).model_dump(mode="json")
    )
    with pytest.raises(ValueError, match="adjudicated"):
        run_benchmark(
            manifest,
            BenchmarkOptions(),
            tmp_path / "run",
            BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1"),
            split="selection",
        )
