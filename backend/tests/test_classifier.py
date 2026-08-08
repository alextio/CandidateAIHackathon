"""Cover for the feature frame and the train -> predict path.

Deliberately not exhaustive. Each test pins one thing that would be silently
wrong rather than loudly broken: a join that drops rows, an ordering that
inverts, a probability that stops meaning what the column comment says.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.classify import store
from app.classify.features import (
    build_feature_frame,
    design_frame,
    is_affirmative,
    snapshots_frame,
)
from app.classify.labels import weak_label
from app.classify.momentum import compute_momentum
from app.classify.stages import Stage
from app.classify.train import MIN_ROWS_FOR_CALIBRATION, predict_rows, to_model_run, train

AS_OF = date(2026, 7, 1)


def make_snapshots(n_projects: int = 120, n_months: int = 6) -> list[dict]:
    """Synthetic ERCOT history spanning four stages.

    Milestones are assigned by index so the rule table produces a spread of
    labels rather than one dominant class — a single-class training set
    exercises none of the ordinal machinery.
    """
    rows: list[dict] = []

    for index in range(n_projects):
        group = index % 4
        for month in range(n_months):
            report_date = AS_OF - timedelta(days=30 * (n_months - 1 - month))
            row = {
                "inr": f"26INR{index:04d}",
                "report_date": report_date,
                "project_name": f"Project {index}",
                "size_category": "Large" if index % 2 else "Small",
                "status": "active",
                "interconnecting_entity": f"Developer {index % 7}",
                "county": ["HARRIS", "HOOD", "WARD", "ECTOR"][index % 4],
                "state": "TX",
                "fuel": ["GAS", "BAT", "GAS", "BAT"][group],
                "technology": ["CC", "BA", "GT", "BA"][group],
                "capacity_mw": 100.0 + 25.0 * group + month,
                "gim_study_phase": None,
                "projected_cod": AS_OF + timedelta(days=365 + 20 * month),
                "ia_signed": None,
                "approved_for_energization": None,
                "approved_for_synchronization": None,
                "inactive_date": None,
                "cancel_date": None,
                "source_sheet": "Large Gen",
            }

            if group == 0:
                row["gim_study_phase"] = "Screening Study"
            elif group == 1:
                row["gim_study_phase"] = "Full Interconnection Study"
            elif group == 2:
                row["gim_study_phase"] = "FIS Approved"
            else:
                row["gim_study_phase"] = "Interconnection Agreement"
                row["ia_signed"] = "2025-01-15"

            rows.append(row)

    return rows


def latest_of(rows: list[dict]) -> list[dict]:
    newest: dict[str, dict] = {}
    for row in rows:
        current = newest.get(row["inr"])
        if current is None or row["report_date"] > current["report_date"]:
            newest[row["inr"]] = row
    return list(newest.values())


@pytest.fixture
def prepared() -> tuple[pl.DataFrame, list]:
    history = make_snapshots()
    latest = latest_of(history)
    momentum = compute_momentum(snapshots_frame(history), AS_OF)
    frame = build_feature_frame(latest, momentum, as_of=AS_OF)
    labels = [weak_label(row, entity_id=f"ercot:{row['inr']}", as_of=AS_OF) for row in latest]
    return frame, labels


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-01-15", True),
        ("Y", True),
        ("N", False),
        ("", False),
        (None, False),
        ("pending", False),
    ],
)
def test_is_affirmative_reads_free_text_milestones(value, expected):
    assert is_affirmative(value) is expected


def test_feature_frame_has_one_row_per_project_and_no_missing_momentum():
    history = make_snapshots(n_projects=10)
    latest = latest_of(history)
    momentum = compute_momentum(snapshots_frame(history), AS_OF)

    frame = build_feature_frame(latest, momentum, as_of=AS_OF)

    assert frame.height == 10
    assert frame["inr"].n_unique() == 10
    # Six snapshots each, so the momentum join must have landed on every row.
    assert frame["n_snapshots"].to_list() == [6.0] * 10


def test_project_without_history_gets_one_snapshot_not_null():
    """A project the momentum frame has never seen must not arrive as null.

    Null would be imputed with the median snapshot count of projects that *do*
    have history, inventing a series that does not exist.
    """
    latest = latest_of(make_snapshots(n_projects=1))
    empty = compute_momentum(pl.DataFrame(schema={"inr": pl.String}), AS_OF)

    frame = build_feature_frame(latest, empty, as_of=AS_OF)

    assert frame.height == 1
    assert frame["n_snapshots"].to_list() == [1.0]
    assert frame["momentum_grade"].to_list() == ["unknown"]


def test_linked_permits_reach_the_feature_row():
    history = make_snapshots(n_projects=2)
    latest = latest_of(history)
    momentum = compute_momentum(snapshots_frame(history), AS_OF)
    inr = latest[0]["inr"]

    frame = build_feature_frame(
        latest,
        momentum,
        as_of=AS_OF,
        linked_permits={
            inr: [{"entity_status": "ACTIVE", "on_thesis": True, "link_score": 0.81}]
        },
    )

    with_permit = frame.filter(pl.col("inr") == inr).to_dicts()[0]
    without = frame.filter(pl.col("inr") != inr).to_dicts()[0]

    assert with_permit["n_permits"] == 1.0
    assert with_permit["has_active_permit"] is True
    assert with_permit["has_thesis_permit"] is True
    assert with_permit["permit_score"] == pytest.approx(0.81)
    assert without["n_permits"] == 0.0
    assert without["has_active_permit"] is False


def test_design_frame_leaves_no_nulls_in_categoricals():
    """OneHotEncoder cannot see a polars null; unlabelled must be an explicit level."""
    latest = latest_of(make_snapshots(n_projects=3))
    for row in latest:
        row["fuel"] = None

    frame = build_feature_frame(
        latest, compute_momentum(pl.DataFrame(schema={"inr": pl.String}), AS_OF), as_of=AS_OF
    )
    pandas_frame = design_frame(frame)

    assert pandas_frame["fuel"].isna().sum() == 0
    assert set(pandas_frame["fuel"]) == {"__missing__"}


# ---------------------------------------------------------------------------
# Train and predict
# ---------------------------------------------------------------------------
def test_training_produces_a_calibrated_model_with_classes_in_lifecycle_order(prepared):
    frame, labels = prepared

    model = train(frame, labels)

    assert model.metrics["calibrated"] is True
    assert model.metrics["n_labelled"] >= MIN_ROWS_FOR_CALIBRATION
    # Ordinal metrics, expected rank and conformal expansion all assume class
    # index i sits next to i+1 on the real lifecycle. sklearn would have sorted
    # these as strings.
    ranks = [stage.rank for stage in model.classes]
    assert ranks == sorted(ranks)
    assert model.conformal is not None
    assert model.coefficients.shape == (len(model.classes), len(model.feature_names))


def test_prediction_rows_carry_a_contiguous_conformal_interval(prepared):
    frame, labels = prepared
    model = train(frame, labels)

    rows = predict_rows(
        model, frame, as_of=AS_OF, rule_labels={label.inr: label for label in labels}
    )

    assert len(rows) == frame.height

    order = [stage.value for stage in Stage]
    for row in rows:
        assert row["stage"] in order
        assert order.index(row["conformal_lo"]) <= order.index(row["conformal_hi"])
        # expected_rank lives on the full Stage scale so it stays comparable
        # across models trained on different surviving classes.
        assert 0.0 <= row["expected_rank"] <= len(order) - 1


def test_probabilities_sum_to_one_and_match_the_reported_confidence(prepared):
    frame, labels = prepared
    model = train(frame, labels)

    probabilities = model.probabilities(frame)
    rows = predict_rows(model, frame, as_of=AS_OF)

    assert np.allclose(probabilities.sum(axis=1), 1.0)
    assert rows[0]["confidence"] == pytest.approx(float(probabilities[0].max()))
    # Objects, not JSON text: the columns are jsonb, and a pre-serialised string
    # would land as a JSON string scalar that every reader has to parse again.
    assert rows[0]["probabilities"].keys() == {stage.value for stage in model.classes}
    assert isinstance(rows[0]["contributions"], list)
    assert isinstance(rows[0]["justification"], list)


def test_training_is_idempotent_on_identical_inputs(prepared):
    """model_version is a content hash, so a re-run must not add a second row."""
    frame, labels = prepared

    assert train(frame, labels).model_version == train(frame, labels).model_version


def test_model_run_row_is_json_serialisable_and_matches_the_coefficients(prepared):
    frame, labels = prepared
    model = train(frame, labels)

    run = to_model_run(model, git_sha="abc1234")

    assert run["n_features"] == len(model.feature_names)
    assert len(run["feature_names"]) == run["n_features"]
    assert np.array(run["coefficients"]).shape == model.coefficients.shape
    assert run["classes"] == [stage.value for stage in model.classes]
    # The jsonb payloads have to survive the trip as JSON. `store` strips the
    # NaNs that `metrics` carries whenever a metric is undefined.
    assert json.dumps(store._json_safe(run), allow_nan=False)


def test_no_sklearn_placeholder_reaches_the_feature_names(prepared):
    """"..._infrequent_sklearn" is a library detail, not an explanation."""
    frame, labels = prepared
    model = train(frame, labels)

    assert not any(name.endswith("_infrequent_sklearn") for name in model.feature_names)


def test_too_few_labels_fits_without_calibration_rather_than_raising():
    history = make_snapshots(n_projects=8)
    latest = latest_of(history)
    momentum = compute_momentum(snapshots_frame(history), AS_OF)
    frame = build_feature_frame(latest, momentum, as_of=AS_OF)
    labels = [weak_label(row, entity_id=row["inr"], as_of=AS_OF) for row in latest]

    model = train(frame, labels)

    assert model.metrics["calibrated"] is False
    assert model.conformal is None
    assert "warning" in model.metrics
    # Still usable — the point of degrading rather than raising.
    assert predict_rows(model, frame, as_of=AS_OF)[0]["conformal_lo"] is None


def test_no_usable_labels_is_a_clear_error(prepared):
    frame, labels = prepared
    for label in labels:
        label.withdrawn = True

    with pytest.raises(ValueError, match="no usable weak labels"):
        train(frame, labels)
