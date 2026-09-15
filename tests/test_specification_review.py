from __future__ import annotations

from repo2rlenv.quality.specification import SpecificationReview


def test_reviewer_cannot_pass_while_still_requesting_repairs():
    finding = {"status": "pass", "explanation": "Looks supported", "evidence": "an excerpt"}
    review = SpecificationReview(
        **{
            name: finding
            for name in ("clarity", "verifier_alignment", "answer_leakage", "reference_validity")
        },
        actionable_repairs=["Disclose the numeric tolerance required by the verifier."],
    )
    assert not review.passed


def test_unknown_reference_evidence_blocks_specification_acceptance():
    finding = {"status": "pass", "explanation": "Looks supported", "evidence": "an excerpt"}
    review = SpecificationReview(
        **{name: finding for name in ("clarity", "verifier_alignment", "answer_leakage")},
        reference_validity={**finding, "status": "unknown"},
        actionable_repairs=[],
    )
    assert not review.passed
