"""Momentum: what the snapshot series says that any single snapshot cannot.

`ercot_projects` is keyed (inr, report_date), so every project carries its own
history. A single row tells you where a project claims to be. The series tells
you whether it is actually moving — which is the question origination cares
about, because a project that has been six months from COD for three years is
not an early-stage opportunity, it is a stalled one.

The load-bearing metric is `cod_slip_rate`: the slope of projected COD against
report date.

    ~0    COD is holding still while time passes    on schedule
    ~1    COD recedes one month per month elapsed   treading water
    >1    COD receding faster than time passes      losing ground
    <0    COD pulling closer                        accelerating

Three deliberate choices:

Theil-Sen rather than least squares. A single wild COD revision — a developer
pushing a date out five years in one filing — is common in this data, and OLS
would let that one point set the slope. Theil-Sen tolerates corruption of up to
about 29% of points in the simple-regression case. scipy returns the confidence
interval with the slope, so no bootstrap is needed.

Null rather than zero below three snapshots. "Not enough history to say" and
"not moving" are different claims, and a dashboard that renders them the same
way is lying. polars keeps that distinction honestly — null is not NaN, and it
survives aggregation without being silently coerced to a number.

Polars rather than pandas. Everything except the Theil-Sen fit itself is
expressible as a grouped expression, so the grouping and the per-project
statistics run in Rust and Python only makes the one call that genuinely needs
scipy. The earlier pandas version ran a Python function per project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl
from scipy.stats import theilslopes

from .phase import PHASE_LADDER, phase_rank_of

# Below this, a slope is not estimated at all. Two points always fit a line
# perfectly and tell you nothing about whether the trend is real.
MIN_SNAPSHOTS_FOR_SLOPE = 3

DAYS_PER_MONTH = 30.4375

# Grade cut points on cod_slip_rate.
ACCELERATING_BELOW = -0.25
ON_TRACK_BELOW = 0.25
SLIPPING_BELOW = 0.85

# A confidence interval wider than this spans several grades at once, so the
# point estimate is not worth reporting as a grade.
MAX_CREDIBLE_CI_WIDTH = 1.5

# Fields whose movement counts as the project doing something.
TRACKED_FIELDS = ("status", "gim_study_phase", "projected_cod", "capacity_mw", "ia_signed")


# ---------------------------------------------------------------------------
# GIM study phase ordering
#
# The parsing lives in `phase.py`, which documents what the live column really
# contains. An earlier version matched patterns as substrings against the whole
# string; because every live value contains "FIS", that returned the same rank
# for all 1,238 projects and made `phase_velocity` identically zero.
# ---------------------------------------------------------------------------
PHASE_RANKS: tuple[tuple[str, int], ...] = PHASE_LADDER


def phase_rank(value: object) -> float:
    """Ordinal position of a GIM study phase, or NaN when unrecognised.

    Kept as a scalar function alongside the polars expression below: the two
    must agree, and `test_phase_rank_expression_matches_the_scalar` checks it.
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return np.nan

    rank = phase_rank_of(str(value))
    return np.nan if rank is None else rank


def phase_rank_expr(column: str = "gim_study_phase") -> pl.Expr:
    """The same mapping as a polars expression.

    Delegates to the scalar rather than re-expressing the segment parser as a
    chain of `when`/`then`: the two would then be two implementations of one
    rule, free to drift, and the drift would be invisible — a wrong rank does
    not raise, it just inverts `phase_velocity` for the projects it touches.

    The per-element call is cheap because `phase.parse_phase` is memoised and
    the column holds six distinct values across the entire queue.
    """
    return (
        pl.col(column)
        .cast(pl.String)
        .map_elements(
            lambda value: phase_rank_of(value) if value is not None else None,
            return_dtype=pl.Float64,
        )
    )


def grade_slope(slope: float | None, low: float | None, high: float | None) -> str:
    """Bucket a slip rate, refusing to grade when the interval is too wide.

    The interval check is the honest part. A point estimate of 0.4 with a
    confidence interval running from -1.2 to 2.0 is compatible with the project
    accelerating and with it being dead; reporting "slipping" from that would
    invent precision the data does not have.
    """
    if slope is None or not np.isfinite(slope):
        return "unknown"

    if low is not None and high is not None and np.isfinite(low) and np.isfinite(high):
        if (high - low) > MAX_CREDIBLE_CI_WIDTH:
            return "unknown"

    if slope < ACCELERATING_BELOW:
        return "accelerating"
    if slope < ON_TRACK_BELOW:
        return "on_track"
    if slope < SLIPPING_BELOW:
        return "slipping"
    return "stalled"


@dataclass
class CodSlip:
    slope: float | None = None
    low: float | None = None
    high: float | None = None
    mad: float | None = None


def cod_slip(report_days: np.ndarray, cod_days: np.ndarray) -> CodSlip:
    """Theil-Sen slope of projected COD against report date, with its interval.

    Both inputs are days since the epoch; the result is in months per month,
    which is the unit the grades are defined in.
    """
    report_days = np.asarray(report_days, dtype=float)
    cod_days = np.asarray(cod_days, dtype=float)

    usable = np.isfinite(report_days) & np.isfinite(cod_days)
    x = report_days[usable] / DAYS_PER_MONTH
    y = cod_days[usable] / DAYS_PER_MONTH

    # Distinct x values, not just distinct rows: several snapshots on one date
    # carry no information about a trend over time.
    if len(x) < MIN_SNAPSHOTS_FOR_SLOPE or len(np.unique(x)) < MIN_SNAPSHOTS_FOR_SLOPE:
        return CodSlip()

    result = theilslopes(y, x)
    slope, intercept = float(result[0]), float(result[1])
    low, high = float(result[2]), float(result[3])

    residuals = y - (intercept + slope * x)
    mad = float(np.median(np.abs(residuals - np.median(residuals))))

    return CodSlip(slope=slope, low=low, high=high, mad=mad)


def _prepare(snapshots: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """Filter to what was knowable at `as_of`, sort, and add helper columns.

    The date filter is the leakage guard. Without it a model labelling the stage
    at a past date reads snapshots published after it — the easiest way to build
    something that scores beautifully and is worthless in production.
    """
    frame = snapshots.with_columns(pl.col("report_date").cast(pl.Date))

    for column in ("projected_cod",):
        if column in frame.columns:
            frame = frame.with_columns(pl.col(column).cast(pl.Date))

    frame = frame.filter(pl.col("report_date") <= pl.lit(as_of))
    if frame.is_empty():
        return frame

    frame = frame.sort("inr", "report_date")

    present = [column for column in TRACKED_FIELDS if column in frame.columns]

    return frame.with_columns(
        pl.int_range(pl.len()).over("inr").alias("_index"),
        pl.col("report_date").dt.epoch("d").alias("_report_days"),
        (
            pl.col("projected_cod").dt.epoch("d")
            if "projected_cod" in frame.columns
            else pl.lit(None, dtype=pl.Int64)
        ).alias("_cod_days"),
        (
            phase_rank_expr()
            if "gim_study_phase" in frame.columns
            else pl.lit(None, dtype=pl.Float64)
        ).alias("_rank"),
        # ne_missing compares nulls as values rather than propagating them, so a
        # field going from null to populated counts as a change.
        (
            pl.any_horizontal(
                [pl.col(column).ne_missing(pl.col(column).shift().over("inr")) for column in present]
            )
            if present
            else pl.lit(False)
        ).alias("_changed_raw"),
    ).with_columns(
        # The first row of a group is not a change — there was nothing before it.
        (pl.col("_changed_raw") & (pl.col("_index") > 0)).alias("_changed")
    )


MOMENTUM_COLUMNS = (
    "inr",
    "as_of",
    "n_snapshots",
    "first_report_date",
    "last_report_date",
    "cod_slip_rate",
    "cod_slip_lo",
    "cod_slip_hi",
    "cod_slip_mad",
    "phase_velocity",
    "capacity_cv",
    "status_changes",
    "days_since_change",
    "momentum_grade",
)

_EMPTY_SCHEMA = {
    "inr": pl.String,
    "as_of": pl.Date,
    "n_snapshots": pl.UInt32,
    "first_report_date": pl.Date,
    "last_report_date": pl.Date,
    "cod_slip_rate": pl.Float64,
    "cod_slip_lo": pl.Float64,
    "cod_slip_hi": pl.Float64,
    "cod_slip_mad": pl.Float64,
    "phase_velocity": pl.Float64,
    "capacity_cv": pl.Float64,
    "status_changes": pl.UInt32,
    "days_since_change": pl.Int64,
    "momentum_grade": pl.String,
}


def compute_momentum(snapshots: pl.DataFrame, as_of: date | None = None) -> pl.DataFrame:
    """Momentum for every project in `snapshots`, as of one date.

    `snapshots` is raw ercot_projects rows. `as_of` defaults to the latest
    report_date present, which is what a live run wants; training passes an
    explicit past date to reconstruct what was knowable then.
    """
    if snapshots.is_empty() or "inr" not in snapshots.columns:
        return pl.DataFrame(schema=_EMPTY_SCHEMA)

    resolved_as_of = as_of or snapshots.select(pl.col("report_date").cast(pl.Date).max()).item()
    if resolved_as_of is None:
        return pl.DataFrame(schema=_EMPTY_SCHEMA)

    frame = _prepare(snapshots, resolved_as_of)
    if frame.is_empty():
        return pl.DataFrame(schema=_EMPTY_SCHEMA)

    has_capacity = "capacity_mw" in frame.columns
    has_status = "status" in frame.columns

    aggregated = frame.group_by("inr", maintain_order=True).agg(
        pl.len().alias("n_snapshots"),
        pl.col("report_date").min().alias("first_report_date"),
        pl.col("report_date").max().alias("last_report_date"),
        # Collected for the Theil-Sen call; dropped before returning.
        pl.col("_report_days").alias("_report_days"),
        pl.col("_cod_days").alias("_cod_days"),
        # Phase velocity endpoints. The frame is date-sorted, so first and last
        # non-null rank are the earliest and latest known phases.
        pl.col("_rank").drop_nulls().first().alias("_first_rank"),
        pl.col("_rank").drop_nulls().last().alias("_last_rank"),
        pl.col("report_date").filter(pl.col("_rank").is_not_null()).first().alias("_first_rank_date"),
        pl.col("report_date").filter(pl.col("_rank").is_not_null()).last().alias("_last_rank_date"),
        (
            pl.when(pl.col("capacity_mw").drop_nulls().len() >= 2)
            .then(pl.col("capacity_mw").std(ddof=0) / pl.col("capacity_mw").mean())
            .otherwise(None)
            if has_capacity
            else pl.lit(None, dtype=pl.Float64)
        ).alias("capacity_cv"),
        (
            (pl.col("status").ne_missing(pl.col("status").shift()) & (pl.col("_index") > 0))
            .sum()
            .cast(pl.UInt32)
            if has_status
            else pl.lit(0, dtype=pl.UInt32)
        ).alias("status_changes"),
        # Date of the most recent change; falls back to first sighting, which is
        # the strongest possible staleness signal rather than a missing one.
        pl.when(pl.col("_changed").any())
        .then(pl.col("report_date").filter(pl.col("_changed")).max())
        .otherwise(pl.col("report_date").min())
        .alias("_last_change_date"),
    )

    # The one irreducibly Python part: scipy per project. Everything above ran
    # in Rust, so this loop touches two small numpy arrays per project and
    # nothing else.
    slips = [
        cod_slip(np.asarray(report_days, dtype=float), np.asarray(cod_days, dtype=float))
        for report_days, cod_days in zip(
            aggregated["_report_days"].to_list(), aggregated["_cod_days"].to_list(), strict=True
        )
    ]

    aggregated = aggregated.with_columns(
        pl.Series("cod_slip_rate", [slip.slope for slip in slips], dtype=pl.Float64),
        pl.Series("cod_slip_lo", [slip.low for slip in slips], dtype=pl.Float64),
        pl.Series("cod_slip_hi", [slip.high for slip in slips], dtype=pl.Float64),
        pl.Series("cod_slip_mad", [slip.mad for slip in slips], dtype=pl.Float64),
        pl.Series(
            "momentum_grade",
            [grade_slope(slip.slope, slip.low, slip.high) for slip in slips],
            dtype=pl.String,
        ),
    )

    years = (
        pl.col("_last_rank_date").cast(pl.Date) - pl.col("_first_rank_date").cast(pl.Date)
    ).dt.total_days() / 365.25

    return (
        aggregated.with_columns(
            pl.lit(resolved_as_of).cast(pl.Date).alias("as_of"),
            pl.when(years > 0)
            .then((pl.col("_last_rank") - pl.col("_first_rank")) / years)
            .otherwise(None)
            .alias("phase_velocity"),
            (pl.lit(resolved_as_of) - pl.col("_last_change_date"))
            .dt.total_days()
            .cast(pl.Int64)
            .alias("days_since_change"),
        )
        .select(MOMENTUM_COLUMNS)
    )
