from __future__ import annotations

import json
from pathlib import Path

from repo2rlenv.curation.funnel import (
    audit_funnel,
    failure_labels,
    render_markdown,
    source_url,
    write_report,
)

A = "https://github.com/org/repo/pull/1"
B = "https://github.com/org/repo/pull/2"
C = "https://github.com/org/other/pull/3"


def put(root: Path, name: str, data: object) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def source(root: Path, name: str, url: str = A) -> Path:
    return put(root, name + "/source.json", {"id": "org-repo-1", "url": url})


def trial(
    root: Path, name: str, label: str, reward: int | None, *, error=None, model=None, physical=None
):
    return put(
        root,
        name + f"/trials/{label}.json",
        {
            "label": label,
            "reward": reward,
            "error": error,
            "model": model,
            "task_digest": "digest1",
            "inference_digest": "policy" if model else None,
            "path": "/old/run/trials/" + (physical or label + "-physical"),
        },
    )


def test_catalog_is_distinct_and_metadata_alone_is_not_an_attempt(tmp_path):
    seeds = tmp_path / "seeds.md"
    seeds.write_text(A + "\n" + A + "?x=1\n" + B + "\n")
    source(tmp_path, "run/one")
    trial(tmp_path, "run/one/drafts/1", "oracle-0", 1)
    source(tmp_path, "metadata", B)
    source(tmp_path, "other", C)
    trial(tmp_path, "other/revision-0", "baseline", 0)
    report = audit_funnel(tmp_path, seed_paths=(seeds,))
    assert report["denominators"] == {
        "seed_catalog_distinct_prs": 2,
        "attempted_catalog_prs": 1,
        "untouched_catalog_prs": 1,
        "attempted_outside_catalog_prs": 1,
        "all_attempted_distinct_prs": 2,
        "metadata_only_distinct_prs": 0,
    }
    assert report["untouched_sources"] == [B]


def test_reused_trials_do_not_double_count_costs_or_physical_trials(tmp_path):
    source(tmp_path, "old")
    source(tmp_path, "resume")
    for name in ("old/revision-0", "resume/revision-0"):
        trial(tmp_path, name, "solver-0-0", 1, model="m")
    entry = {"scope": "stage2:" + A, "charged_usd": 2.0, "status": "metered"}
    one = put(tmp_path, "a/budget.json", {"entries": {"same": entry}})
    two = put(tmp_path, "b/budget.json", {"entries": {"same": entry}})
    report = audit_funnel(tmp_path, ledger_paths=(one, two))
    assert report["counts"]["unique_physical_trials"] == 1
    assert len(report["trials"][0]["evidence_paths"]) == 2
    assert report["costs"]["unique_entry_ids"] == 1
    assert report["costs"]["committed_usd"] == 2
    assert report["costs"]["totals"] == {"metered_usd": 2}


def test_reserved_estimates_and_conflicting_copies_are_explicit(tmp_path):
    one = put(
        tmp_path,
        "live/budget.json",
        {
            "entries": {
                "a": {"scope": A, "charged_usd": 1, "status": "metered"},
                "b": {"scope": A, "charged_usd": 2, "status": "estimated"},
                "c": {"scope": "unknown", "charged_usd": 3, "status": "reserved"},
            }
        },
    )
    two = put(
        tmp_path,
        "copy/budget.json",
        {
            "entries": {
                "a": {"scope": A, "charged_usd": 9, "status": "reserved"},
            }
        },
    )
    report = audit_funnel(tmp_path, ledger_paths=(one, two))
    assert report["costs"]["totals"] == {
        "metered_usd": 1,
        "estimated_usd": 2,
        "outstanding_reserved_usd": 3,
    }
    assert report["costs"]["committed_usd"] == 6
    assert len(report["costs"]["conflicting_copies"]) == 1
    assert report["costs"]["unallocated_entries"][0]["entry_id"] == "c"


def test_raw_acceptance_is_separate_from_selection_and_deduplicated(tmp_path):
    source(tmp_path, "run")
    accepted = {"source": A, "status": "accepted", "task_digest": "one"}
    put(tmp_path, "run/result.json", accepted)
    put(tmp_path, "run/manifest.json", {"accepted": [accepted]})
    selected = put(
        tmp_path,
        "selection.json",
        {"selected": [{"source": B, "task_digest": "two", "human_review": "pending"}]},
    )
    report = audit_funnel(tmp_path, selection_paths=(selected,))
    assert report["counts"]["raw_autoaccepted_digests"] == 1
    assert report["counts"]["selected_digests"] == 1
    assert report["raw_autoacceptances"][0]["source"] == A
    assert report["selected"][0]["source"] == B
    assert report["selected"][0]["human_review"] == "pending"


def test_controls_are_not_misclassified_as_environment_failures(tmp_path):
    source(tmp_path, "run")
    trial(tmp_path, "run/revision-0", "mutation-good", 0)
    trial(tmp_path, "run/revision-0", "mutation-survives", 1)
    trial(tmp_path, "run/revision-0", "equivalent-rejected", 0)
    trial(tmp_path, "run/revision-0", "solver-0-0", 0, model="m")
    trial(tmp_path, "run/revision-0", "adversary", 1, model="m")
    report = audit_funnel(tmp_path)
    labels = report["failure_label_counts"]
    assert labels == {
        "negative_control_survived": 1,
        "valid_alternative_rejected": 1,
        "solver_unsolved": 1,
        "adversary_rewarded_requires_trace_review": 1,
    }


def test_multiple_failure_causes_and_unknown_preserved(tmp_path):
    source(tmp_path, "run")
    trial(
        tmp_path, "run/revision-0", "oracle-0", None, error="BudgetExceeded after sandbox timeout"
    )
    trial(tmp_path, "run/revision-0", "solver-0-0", None, error="unclassified oddity")
    report = audit_funnel(tmp_path)
    assert report["failure_label_counts"] == {"budget": 1, "infrastructure": 1, "unknown": 1}
    assert failure_labels("coverage and ambiguous instruction") == [
        "specification",
        "verifier_coverage",
    ]


def test_repair_provenance_attribution_and_scope_mapping(tmp_path):
    original = source(tmp_path, "old")
    prov = {
        "source_path": str(original),
        "original_task_digest": "old",
        "repaired_task_digest": "new",
    }
    put(tmp_path, "repo-repair/provenance-v1.json", prov)
    put(tmp_path, "repo-repair/provenance-v2.json", prov)
    put(
        tmp_path,
        "repo-repair/validation/manifest.json",
        {"budget_scope": "repair:repo", "accepted": []},
    )
    trial(tmp_path, "repo-repair/validation/revision-0", "oracle-0", 1)
    ledger = put(
        tmp_path,
        "live/budget.json",
        {"entries": {"id": {"scope": "repair:repo", "charged_usd": 4, "status": "estimated"}}},
    )
    report = audit_funnel(tmp_path, ledger_paths=(ledger,))
    assert report["counts"]["recorded_manual_repair_transitions"] == 1
    assert report["sources"][0]["costs"] == {"estimated_usd": 4}


def test_heavy_untrusted_trees_and_large_submission_are_never_read(tmp_path, monkeypatch):
    source(tmp_path, "run")
    for name in (
        "node_modules/bad/source.json",
        "production-runtime-v17/bad/source.json",
        "run/revision-0/trials/a/artifacts/source.json",
        "run/prior-review/source.json",
    ):
        put(tmp_path, name, {"url": B})
    put(tmp_path, "run/review-submissions.json", {"url": B})
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert not any(
            x in str(path)
            for x in (
                "node_modules",
                "runtime-v17",
                "artifacts",
                "prior-review",
                "review-submissions",
            )
        )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    report = audit_funnel(tmp_path)
    assert [x["source"] for x in report["sources"]] == [A]


def test_malformed_evidence_is_reported_without_mutating_inputs(tmp_path):
    path = tmp_path / "result.json"
    path.write_text('{"broken":')
    before = path.read_bytes()
    report = audit_funnel(tmp_path)
    assert report["skipped_inputs"][0]["path"] == str(path)
    assert path.read_bytes() == before
    outputs = write_report(report, tmp_path / "systematic-reset")
    assert all(p.exists() for p in outputs)
    assert "not invoices" in render_markdown(report)


def test_url_normalization():
    assert source_url("HTTPS://github.com/Org/Repo/pull/001#issue") == A
    assert source_url("not a PR") is None


def test_runtime_comparison_rows_and_verdict_are_real_evidence(tmp_path):
    accepted = {"source": A, "status": "accepted", "task_digest": "d"}
    put(
        tmp_path,
        "runtime-comparison/comparison.json",
        {
            "rows": [accepted],
            "previous_attempts": [
                {"source": B, "status": "infrastructure_failure", "error": "sandbox timeout"}
            ],
        },
    )
    put(tmp_path, "runtime-comparison/candidates/a/verdict.json", accepted)
    put(tmp_path, "comparison-runtime-v5/result.json", {"source": C, "status": "accepted"})
    r = audit_funnel(tmp_path)
    assert r["counts"]["raw_autoaccepted_digests"] == 1
    assert r["denominators"]["all_attempted_distinct_prs"] == 2
    assert r["failure_label_counts"] == {"infrastructure": 1}


def test_cost_only_attempt_and_unique_legacy_repair_attribution(tmp_path):
    seeds = tmp_path / "seeds.md"
    seeds.write_text(A + "\n" + B)
    ledger = put(
        tmp_path,
        "run/budget.json",
        {
            "entries": {
                "one": {"scope": "repair:repo-1-v2", "charged_usd": 3, "status": "metered"},
                "two": {"scope": "diagnostic:unrelated", "charged_usd": 2, "status": "reserved"},
            }
        },
    )
    r = audit_funnel(tmp_path, seed_paths=(seeds,), ledger_paths=(ledger,))
    assert r["denominators"]["attempted_catalog_prs"] == 1
    assert r["untouched_sources"] == [B]
    assert r["sources"][0]["costs"] == {"metered_usd": 3}
    assert (
        r["costs"]["entries"]["one"]["attribution"]
        == "unique repository basename and PR number in repair scope"
    )
    assert len(r["costs"]["unallocated_entries"]) == 1


def test_malformed_optional_manifest_shapes_do_not_crash(tmp_path):
    put(tmp_path, "run/manifest.json", {"seeds": None, "budget_scopes": [], "accepted": {}})
    put(tmp_path, "run/review.json", {"criteria": None})
    assert audit_funnel(tmp_path)["denominators"]["all_attempted_distinct_prs"] == 0


def test_focused_review_cache_copies_deduplicate_without_becoming_final_reviews(tmp_path):
    source(tmp_path, "old")
    source(tmp_path, "resume")
    for name in ["old", "resume"]:
        put(
            tmp_path,
            name + "/verifier-reviews/hash/result.json",
            {
                "status": "completed",
                "review": {"score": 2, "blockers": ["missing independent expectation"]},
            },
        )
    r = audit_funnel(tmp_path)
    assert r["counts"]["unique_verifier_review_identities"] == 1
    assert r["stage_distinct_sources"]["verifier_review"] == 1
    assert r["stage_distinct_sources"]["verifier_review_passed"] == 0
    assert r["stage_distinct_sources"]["final_review"] == 0
    assert r["failure_label_counts"]["verifier_review_rejected"] == 1
