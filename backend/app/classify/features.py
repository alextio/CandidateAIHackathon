"""The feature frame: one row per project, built from what was knowable at `as_of`.

Three inputs are joined here — the newest ERCOT snapshot, the momentum series
computed from that project's full history, and any TCEQ permits linked to it —
because each answers a different question:

    snapshot   where does the project claim to be right now
    momentum   is it actually moving, and how fast
    permits    has anyone spent real money on it yet

A note on what this cannot escape. The weak labels come from the deterministic
rule table, and the rules read `ia_signed`, `gim_study_phase` and the
energization dates — which are also features here. A model trained on this will
partly be relearning the rules, and its agreement with them is not evidence that
it is right. What it adds over the rules is (a) calibrated probabilities and
conformal intervals where the rules give a flat confidence, (b) a usable answer
for the projects no rule fires on, and (c) the momentum and permit signals the
rules never see. `metrics["rule_agreement"]` is reported so that overlap stays
visible rather than being quietly claimed as accuracy.

The preprocessing is a stock scikit-learn `ColumnTransformer`. An earlier draft
hand-rolled the imputation, scaling and vocabularies so a model could be rebuilt
from the database alone with no pickle. That bought reproducibility-from-SQL and
cost about 150 lines of re-implemented sklearn, so it was dropped: the fitted
pipeline is persisted with joblib instead, and the coefficients and feature
names still go to `model_runs` so the dashboard can explain a prediction without
loading anything.

Everything in this module is pure. Nothing here touches a database.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import polars as pl
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .momentum import DAYS_PER_MONTH, phase_rank

# Categorical values seen fewer times than this at fit time are folded into a
# single "infrequent" level. A level with three examples contributes a
# coefficient fitted on three examples, which is noise wearing a feature's
# clothes.
MIN_CATEGORY_COUNT = 25

NUMERIC_COLUMNS = (
    "capacity_mw",
    "log_capacity",
    "phase_rank",
    "cod_horizon_months",
    "queue_age_months",
    "n_snapshots",
    "cod_slip_rate",
    "phase_velocity",
    "capacity_cv",
    "status_changes",
    "days_since_change",
    "n_permits",
    "permit_score",
)

# Booleans pass through unscaled — they have no missing state (absence is False)
# and are already on a comparable scale.
BOOLEAN_COLUMNS = (
    "has_ia_signed",
    "has_energization_date",
    "has_synchronization_date",
    "has_active_permit",
    "has_thesis_permit",
)

# Deliberately short: every extra level is a column in the design matrix, and
# the ordinal signal lives in the numerics.
CATEGORICAL_COLUMNS = ("status", "fuel", "technology", "size_category", "momentum_grade")

MISSING_LEVEL = "__missing__"

# ia_signed is free text in the source. These are the values that mean "no".
_NEGATIVE = {"", "n", "no", "false", "0", "none", "n/a", "na", "-", "tbd", "pending"}


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def is_affirmative(value: Any) -> bool:
    """Whether a free-text milestone field asserts the milestone happened.

    A date counts as affirmative. So does any text that is not one of the
    recognised negatives — erring toward True because the source writes real
    dates and "Y" far more often than it writes creative denials, and a missed
    affirmative silently drags a project backward one stage.
    """
    if value is None:
        return False
    if isinstance(value, (date, datetime)):
        return True
    return str(value).strip().lower() not in _NEGATIVE


def snapshots_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Snapshot rows as a typed polars frame, ready for `compute_momentum`.

    Built column-wise from an explicit schema rather than inferred, because
    polars infers the dtype of an all-null column as Null and every downstream
    date cast then fails on a corner that only appears in small extracts.
    """
    schema = {
        "inr": pl.String,
        "report_date": pl.Date,
        "status": pl.String,
        "gim_study_phase": pl.String,
        "projected_cod": pl.Date,
        "capacity_mw": pl.Float64,
        "ia_signed": pl.String,
    }

    data: dict[str, list[Any]] = {name: [] for name in schema}
    for row in rows:
        data["inr"].append(str(row.get("inr") or ""))
        data["report_date"].append(_as_date(row.get("report_date")))
        data["status"].append(row.get("status"))
        data["gim_study_phase"].append(row.get("gim_study_phase"))
        data["projected_cod"].append(_as_date(row.get("projected_cod")))
        capacity = row.get("capacity_mw")
        data["capacity_mw"].append(float(capacity) if capacity is not None else None)
        ia_signed = row.get("ia_signed")
        data["ia_signed"].append(None if ia_signed is None else str(ia_signed))

    return pl.DataFrame(data, schema=schema)


def build_feature_frame(
    latest: list[dict[str, Any]],
    momentum: pl.DataFrame,
    *,
    as_of: date,
    linked_permits: dict[str, list[dict[str, Any]]] | None = None,
) -> pl.DataFrame:
    """One row per project: raw features, before imputation or encoding.

    Returned as a frame rather than a matrix so the intermediate is inspectable
    — "why did this project get that stage" is answered by looking at these
    columns, and a matrix has already thrown the names away.
    """
    permits_by_inr = linked_permits or {}
    records: list[dict[str, Any]] = []

    for row in latest:
        inr = str(row.get("inr") or "")
        permits = permits_by_inr.get(inr, [])
        capacity = row.get("capacity_mw")
        capacity = float(capacity) if capacity is not None else None

        projected_cod = _as_date(row.get("projected_cod"))
        cod_horizon = (projected_cod - as_of).days / DAYS_PER_MONTH if projected_cod else None

        rank = phase_rank(row.get("gim_study_phase"))

        active_permits = [
            permit
            for permit in permits
            if str(permit.get("entity_status") or "").strip().lower()
            in {"active", "a", "issued"}
        ]

        records.append(
            {
                "inr": inr,
                "capacity_mw": capacity,
                "log_capacity": float(np.log1p(capacity)) if capacity and capacity > 0 else None,
                "phase_rank": None if np.isnan(rank) else float(rank),
                "cod_horizon_months": cod_horizon,
                "queue_age_months": None,  # filled from momentum's first_report_date
                "has_ia_signed": is_affirmative(row.get("ia_signed")),
                "has_energization_date": _as_date(row.get("approved_for_energization")) is not None,
                "has_synchronization_date": _as_date(row.get("approved_for_synchronization"))
                is not None,
                "n_permits": float(len(permits)),
                "permit_score": max(
                    (float(permit.get("link_score") or 0.0) for permit in permits), default=0.0
                ),
                "has_active_permit": bool(active_permits),
                "has_thesis_permit": any(bool(permit.get("on_thesis")) for permit in permits),
                "status": row.get("status"),
                "fuel": row.get("fuel"),
                "technology": row.get("technology"),
                "size_category": row.get("size_category"),
            }
        )

    frame = (
        pl.DataFrame(records)
        if records
        else pl.DataFrame({"inr": []}, schema={"inr": pl.String})
    )

    momentum_columns = [
        column
        for column in (
            "n_snapshots",
            "cod_slip_rate",
            "phase_velocity",
            "capacity_cv",
            "status_changes",
            "days_since_change",
            "momentum_grade",
            "first_report_date",
        )
        if column in momentum.columns
    ]

    if momentum_columns and not momentum.is_empty():
        frame = frame.join(momentum.select(["inr", *momentum_columns]), on="inr", how="left")

    # Missing momentum means one snapshot, not zero project. Fill the count
    # rather than leaving a null that the imputer would later replace with a
    # median drawn from projects that do have history.
    if "n_snapshots" in frame.columns:
        frame = frame.with_columns(pl.col("n_snapshots").fill_null(1).cast(pl.Float64))
    else:
        frame = frame.with_columns(pl.lit(1.0).alias("n_snapshots"))

    if "first_report_date" in frame.columns:
        frame = frame.with_columns(
            (
                (pl.lit(as_of) - pl.col("first_report_date").cast(pl.Date)).dt.total_days()
                / DAYS_PER_MONTH
            ).alias("queue_age_months")
        ).drop("first_report_date")

    if "momentum_grade" in frame.columns:
        frame = frame.with_columns(pl.col("momentum_grade").fill_null("unknown"))
    else:
        frame = frame.with_columns(pl.lit("unknown").alias("momentum_grade"))

    for column in NUMERIC_COLUMNS:
        frame = (
            frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
            if column not in frame.columns
            else frame.with_columns(pl.col(column).cast(pl.Float64))
        )

    for column in BOOLEAN_COLUMNS:
        frame = (
            frame.with_columns(pl.lit(False).alias(column))
            if column not in frame.columns
            else frame.with_columns(pl.col(column).fill_null(False).cast(pl.Boolean))
        )

    for column in CATEGORICAL_COLUMNS:
        frame = (
            frame.with_columns(pl.lit(None, dtype=pl.String).alias(column))
            if column not in frame.columns
            else frame.with_columns(pl.col(column).cast(pl.String))
        )

    return frame.select(["inr", *NUMERIC_COLUMNS, *BOOLEAN_COLUMNS, *CATEGORICAL_COLUMNS])


def design_frame(frame: pl.DataFrame) -> pd.DataFrame:
    """The feature frame in the shape the ColumnTransformer expects.

    sklearn wants pandas here, not polars: `ColumnTransformer` selects by column
    name and `OneHotEncoder` wants a stable string dtype, and polars' null is
    not the NaN/None that `SimpleImputer` looks for.

    Built column-wise from Python lists rather than through `to_pandas()`, which
    would drag in pyarrow for a conversion of a few dozen columns.
    """
    data: dict[str, Any] = {}

    for column in NUMERIC_COLUMNS:
        data[column] = pd.Series(frame[column].to_list(), dtype="float64")

    for column in BOOLEAN_COLUMNS:
        data[column] = pd.Series(
            [bool(value) for value in frame[column].fill_null(False).to_list()], dtype="float64"
        )

    for column in CATEGORICAL_COLUMNS:
        data[column] = pd.Series(
            [
                (str(value).strip().lower() or MISSING_LEVEL)
                if value is not None
                else MISSING_LEVEL
                for value in frame[column].to_list()
            ],
            dtype="object",
        )

    return pd.DataFrame(data)


def make_preprocessor(*, min_category_count: int = MIN_CATEGORY_COUNT) -> ColumnTransformer:
    """Impute, scale, and one-hot encode.

    `add_indicator=True` is the part worth reading twice. Imputing alone would
    tell the model that a project with no projected COD is an average project;
    the indicator lets it learn that the absence itself is informative, which
    for this data it very much is.

    `handle_unknown="infrequent_if_exist"` means a category never seen in
    training lands in the infrequent bucket instead of raising at predict time —
    ERCOT adds status and technology codes between reports.
    """
    numeric = Pipeline(
        [
            ("impute", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
        ]
    )

    categorical = OneHotEncoder(
        handle_unknown="infrequent_if_exist",
        min_frequency=min_category_count,
        sparse_output=False,
    )

    return ColumnTransformer(
        [
            ("numeric", numeric, list(NUMERIC_COLUMNS)),
            ("boolean", "passthrough", list(BOOLEAN_COLUMNS)),
            ("categorical", categorical, list(CATEGORICAL_COLUMNS)),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )
