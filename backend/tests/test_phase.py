"""The `gim_study_phase` parser, pinned against the values ERCOT really writes.

Every string in `LIVE_PHASES` was taken from the July 2026 GIS Report. They are
here because the previous substring-scanning parser accepted all of them without
error and returned the same answer for every one — a silent failure that made
`phase_rank` a constant and `phase_velocity` identically zero across the queue.
"""
from __future__ import annotations

import math

import polars as pl
import pytest

from app.classify.labels import milestones_from_snapshot
from app.classify.momentum import phase_rank, phase_rank_expr
from app.classify.phase import parse_phase, phase_milestones, phase_rank_of

# value -> observed count in the July 2026 report, for the record.
LIVE_PHASES = {
    "SS Completed, FIS Started, No IA": 652,
    "SS Completed, FIS Completed, IA": 150,
    "SS Completed, FIS Started, IA": 107,
    "SS Completed, FIS Completed, No IA": 77,
    "SS Started, FIS Started, No IA": 40,
}


def test_live_phases_do_not_all_collapse_to_one_rank():
    """The bug this module exists to prevent.

    Not one rank per string: the rank is the furthest milestone reached, so
    "SS Started, FIS Started" and "SS Completed, FIS Started" share one. What
    matters is that the column discriminates at all — it previously returned
    2.0 for all 1,238 projects.
    """
    ranks = {phase_rank_of(value) for value in LIVE_PHASES}

    assert len(ranks) > 1, ranks
    assert None not in ranks


def test_the_full_milestone_set_does_distinguish_every_live_phase():
    """What the rank flattens, the milestones keep.

    Compared as milestone names only — the evidence text differs between any two
    inputs, so including it would make this pass without testing anything.
    """
    parsed = {frozenset(phase_milestones(value)) for value in LIVE_PHASES}

    assert len(parsed) == len(LIVE_PHASES)


def test_live_phases_rank_in_lifecycle_order():
    ordered = [
        "SS Started, FIS Started, No IA",
        "SS Completed, FIS Started, No IA",
        "SS Completed, FIS Completed, No IA",
        "SS Completed, FIS Started, IA",
        "SS Completed, FIS Completed, IA",
    ]
    ranks = [phase_rank_of(value) for value in ordered]

    assert ranks == sorted(ranks), dict(zip(ordered, ranks, strict=True))


@pytest.mark.parametrize("value", list(LIVE_PHASES))
def test_no_ia_is_read_as_a_negation(value):
    """"No IA" means there is no agreement, not that the string mentions one."""
    has_ia = "ia_signed" in phase_milestones(value)

    assert has_ia is ("no ia" not in value.lower())


def test_every_segment_is_read_not_just_the_first():
    milestones = phase_milestones("SS Completed, FIS Completed, IA")

    assert milestones.keys() == {
        "screening_study_started",
        "screening_study_complete",
        "fis_requested",
        "fis_approved",
        "ia_signed",
    }


def test_a_completed_milestone_implies_its_own_start():
    assert "fis_requested" in phase_milestones("FIS Completed")
    assert "screening_study_started" in phase_milestones("SS Completed")


def test_bare_abbreviations_do_not_match_inside_longer_words():
    """`\\bia\\b` and `\\bss\\b`, not a naked substring scan."""
    assert phase_milestones("Financial assessment") == {}


def test_long_form_wording_still_parses():
    """ERCOT's process documents spell these out; the report abbreviates them."""
    assert "fis_approved" in phase_milestones("Full Interconnection Study Approved")
    assert "fis_requested" in phase_milestones("Full Interconnection Study")
    assert "screening_study_complete" in phase_milestones("Screening Study Complete")
    assert "ia_signed" in phase_milestones("Interconnection Agreement")


@pytest.mark.parametrize("value", ["", "   ", None, "no idea what this is"])
def test_unrecognised_values_get_no_rank_rather_than_a_guess(value):
    assert phase_rank_of(value) is None


def test_typed_columns_win_over_the_phase_text():
    """A dated column is reported; "IA" in the phase string summarises it."""
    milestones = milestones_from_snapshot(
        {"ia_signed": "2025-01-15", "gim_study_phase": "SS Completed, FIS Completed, IA"}
    )

    assert milestones["ia_signed"] == "2025-01-15"


def test_phase_rank_expression_matches_the_scalar():
    values = [*LIVE_PHASES, "Screening Study", "FIS Approved", None, ""]
    frame = pl.DataFrame({"gim_study_phase": values}, schema={"gim_study_phase": pl.String})

    from_expr = frame.select(phase_rank_expr().alias("rank"))["rank"].to_list()
    from_scalar = [phase_rank(value) for value in values]

    for expr_value, scalar_value in zip(from_expr, from_scalar, strict=True):
        if expr_value is None:
            assert math.isnan(scalar_value)
        else:
            assert expr_value == scalar_value


def test_parse_is_cached_and_returns_a_hashable_result():
    first = parse_phase("SS Completed, FIS Started, No IA")

    assert hash(first)
    assert first is parse_phase("SS Completed, FIS Started, No IA")
