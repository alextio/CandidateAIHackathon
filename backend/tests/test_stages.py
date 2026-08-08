"""The rule table and the weak labels it produces.

Stdlib-only modules, so these run in milliseconds and pin the part of the
classifier that has to stay explainable to a non-technical reader.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.classify.labels import (
    MIN_LABEL_CONFIDENCE,
    is_withdrawn,
    label_quality,
    milestones_from_snapshot,
    permit_stage_evidence,
    weak_label,
)
from app.classify.stages import Stage, infer_stage

AS_OF = date(2026, 7, 1)


def snapshot(**overrides):
    row = {
        "inr": "26INR0001",
        "report_date": AS_OF,
        "project_name": "Test Project",
        "status": "active",
        "gim_study_phase": None,
        "projected_cod": None,
        "ia_signed": None,
        "approved_for_energization": None,
        "approved_for_synchronization": None,
        "cancel_date": None,
        "inactive_date": None,
    }
    row.update(overrides)
    return row


def test_stage_ranks_run_earliest_to_latest():
    ranks = [stage.rank for stage in Stage]

    assert ranks == sorted(ranks)
    assert Stage.CONCEPT.rank < Stage.FEED.rank < Stage.COD.rank


@pytest.mark.parametrize(
    ("milestones", "expected"),
    [
        ({}, Stage.CONCEPT),
        ({"screening_study_started": "2025-02-01"}, Stage.FEL1),
        ({"fis_requested": "2025-03-01"}, Stage.FEL2_PREFEED),
        ({"fis_approved": "2025-06-01"}, Stage.FEED),
        ({"ia_signed": "2025-09-01"}, Stage.INTERCONNECTION_AGREEMENT),
        ({"ia_signed": "2025-09-01", "financial_security_ntp": "Y"}, Stage.FID),
        ({"approved_for_energization": "2026-04-01"}, Stage.COD),
    ],
)
def test_each_rule_fires_on_its_own_evidence(milestones, expected):
    assert infer_stage(milestones).stage is expected


def test_latest_stage_wins_when_several_rules_match():
    """The table is ordered latest-first, so a signed IA does not mask an energization."""
    inference = infer_stage(
        {"ia_signed": "2025-09-01", "approved_for_energization": "2026-04-01"}
    )

    assert inference.stage is Stage.COD


def test_fid_needs_an_affirmative_not_merely_a_present_field():
    """A blank or negative NTP value must not read as money committed."""
    assert infer_stage({"ia_signed": "x", "financial_security_ntp": "N"}).stage is (
        Stage.INTERCONNECTION_AGREEMENT
    )
    assert infer_stage({"ia_signed": "x", "financial_security_ntp": "Yes"}).stage is Stage.FID


def test_confidence_rises_with_corroborating_evidence():
    thin = infer_stage({"ia_signed": "2025-09-01"})
    thick = infer_stage(
        {
            "ia_signed": "2025-09-01",
            "screening_study_started": "2025-01-01",
            "fis_requested": "2025-03-01",
            "fis_approved": "2025-06-01",
            "projected_cod": "2027-01-01",
        }
    )

    assert thick.confidence > thin.confidence
    assert infer_stage({}).confidence < MIN_LABEL_CONFIDENCE


def test_gim_phase_text_maps_to_the_milestone_it_stands_in_for():
    """Live values are decorated, so matching has to be longest-substring-first."""
    assert "fis_approved" in milestones_from_snapshot(
        snapshot(gim_study_phase="Full Interconnection Study Approved")
    )
    assert "fis_requested" in milestones_from_snapshot(
        snapshot(gim_study_phase="Full Interconnection Study")
    )
    assert "screening_study_complete" in milestones_from_snapshot(
        snapshot(gim_study_phase="Screening Study Complete")
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"cancel_date": "2026-02-01"},
        {"inactive_date": "2026-02-01"},
        {"status": "cancelled"},
        {"status": "inactive"},
    ],
)
def test_withdrawal_is_detected_from_any_of_its_three_signals(overrides):
    assert is_withdrawn(snapshot(**overrides)) is True


def test_withdrawn_projects_are_never_trained_on():
    """Their last stage says where they stopped, not where a live project sits."""
    label = weak_label(
        snapshot(ia_signed="2025-09-01", cancel_date="2026-02-01"),
        entity_id="ercot:26INR0001",
        as_of=AS_OF,
    )

    assert label.stage is Stage.INTERCONNECTION_AGREEMENT
    assert label.withdrawn is True
    assert label.usable is False


def test_an_active_permit_supplies_the_fid_evidence_ercot_lacks():
    permits = [
        {"entity_status": "ACTIVE", "affiliation_begin_date": "2025-05-01", "on_thesis": True}
    ]

    assert permit_stage_evidence(permits, AS_OF) == "financial_security_ntp"

    label = weak_label(
        snapshot(ia_signed="2025-09-01"),
        entity_id="ercot:26INR0001",
        as_of=AS_OF,
        linked_permits=permits,
    )
    assert label.stage is Stage.FID


@pytest.mark.parametrize(
    "permit",
    [
        {"entity_status": "INACTIVE", "affiliation_begin_date": "2025-05-01"},
        {"entity_status": "ACTIVE", "affiliation_begin_date": None},
        # Begins after as_of: a pending application is not a commitment.
        {"entity_status": "ACTIVE", "affiliation_begin_date": "2026-11-01"},
        # Already ended before as_of.
        {
            "entity_status": "ACTIVE",
            "affiliation_begin_date": "2024-01-01",
            "affiliation_end_date": "2025-06-01",
        },
    ],
)
def test_permit_evidence_stays_conservative(permit):
    assert permit_stage_evidence([permit], AS_OF) is None


def test_label_quality_names_the_classes_that_can_never_be_predicted():
    """A class with no labels must be reported, not left looking merely rare."""
    labels = [
        weak_label(
            snapshot(inr=f"26INR{index:04d}", ia_signed="2025-09-01"),
            entity_id=f"ercot:26INR{index:04d}",
            as_of=AS_OF,
        )
        for index in range(5)
    ]

    quality = label_quality(labels)

    assert quality.n_usable == 5
    assert Stage.CONSTRUCTION.value in quality.missing_stages
    assert Stage.INTERCONNECTION_AGREEMENT.value not in quality.missing_stages
    assert "NO LABELS for" in quality.report()
