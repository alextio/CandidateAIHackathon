"""Fit, calibrate, and score the stage classifier.

The estimator is deliberately boring: multinomial logistic regression on a few
dozen features. Three reasons it is the right boring choice here.

1. The labels are weak. They come from a rule table, not from humans, so their
   noise floor is well above the gap between a linear model and a boosted one.
   Spending capacity to fit noise more precisely is not an improvement.
2. Every prediction has to be explainable to someone deciding where to send a
   salesperson. `coefficient x standardized feature` is an explanation you can
   read; a SHAP value over 400 trees is a number you have to trust.
3. Logits come out directly, which is what temperature scaling and the conformal
   calibrator both consume.

The pipeline is: fit on train, learn temperature on calibration, learn the
conformal interval width on calibration, and report every metric on a test split
neither of those touched. Fitting temperature on the training logits returns
about 1.0 and calibrates nothing, so the three-way split is load-bearing rather
than ceremonial.

Nothing is persisted to disk. `POST /classify/run` fits and scores in one pass,
so the fitted pipeline only has to outlive the request. Coefficients, feature
names and metrics go to `model_runs` so a dashboard can explain a prediction
straight from SQL; `model_version` is a content hash of the training inputs, so
re-running on unchanged data reproduces the same model rather than accumulating
near-identical rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import cohen_kappa_score, f1_score, log_loss
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from .confidence import (
    Confidence,
    ConformalCalibrator,
    expected_calibration_error,
    fit_conformal,
    fit_temperature,
    softmax,
    summarize,
)
from .features import design_frame, make_preprocessor
from .labels import WeakLabel
from .stages import Stage

ALGO = "multinomial_logistic_regression"

# What OneHotEncoder(min_frequency=...) names the bucket it folds rare levels
# into. Renamed on the way out; see `TrainedModel.feature_names`.
INFREQUENT_SUFFIX = "_infrequent_sklearn"

DEFAULT_ALPHA = 0.10
DEFAULT_SEED = 20260808

# Calibration gets as much as test because both temperature and the conformal
# quantile are estimated from it, and the conformal guarantee degrades with a
# small calibration set in a way accuracy does not.
CALIBRATION_FRACTION = 0.25
TEST_FRACTION = 0.25

# Below this many usable labels the split leaves calibration sets too small for
# either calibrator to mean anything, so we fit but decline to calibrate.
MIN_ROWS_FOR_CALIBRATION = 60

TOP_CONTRIBUTIONS = 6


@dataclass
class TrainedModel:
    """A fitted, calibrated model."""

    model_version: str
    pipeline: Pipeline
    classes: tuple[Stage, ...]
    temperature: float = 1.0
    conformal: ConformalCalibrator | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    n_train: int = 0
    notes: str = ""

    @property
    def feature_names(self) -> list[str]:
        """Design-matrix column names, in matrix order.

        These reach the API verbatim inside `contributions`, so sklearn's
        internal placeholder for the folded-together rare categories is renamed:
        "momentum_grade_infrequent_sklearn" is a library implementation detail
        leaking into a user-facing explanation.
        """
        return [
            str(name).replace(INFREQUENT_SUFFIX, "_other")
            for name in self.pipeline.named_steps["preprocess"].get_feature_names_out()
        ]

    @property
    def coefficients(self) -> np.ndarray:
        return _expand(np.asarray(self.pipeline.named_steps["model"].coef_, dtype=float))

    @property
    def intercepts(self) -> np.ndarray:
        return _expand(np.asarray(self.pipeline.named_steps["model"].intercept_, dtype=float))

    def design(self, frame: pl.DataFrame) -> np.ndarray:
        """The transformed matrix, for explanations."""
        return np.asarray(
            self.pipeline.named_steps["preprocess"].transform(design_frame(frame)), dtype=float
        )

    def probabilities(self, frame: pl.DataFrame) -> np.ndarray:
        """Calibrated class probabilities."""
        return softmax(_logits(self.pipeline, frame), self.temperature)


def _expand(values: np.ndarray) -> np.ndarray:
    """Binary coefficient/intercept arrays as their two-class equivalent.

    sklearn stores one row for two classes. Left unhandled, every downstream
    `coefficients[class_index]` is off by one class.
    """
    if values.ndim == 1:
        return np.array([-values[0], values[0]], dtype=float)
    if values.shape[0] == 1:
        return np.vstack([-values[0], values[0]])
    return values


def _logits(pipeline: Pipeline, frame: pl.DataFrame) -> np.ndarray:
    """Decision function as an (n, K) array regardless of how many classes."""
    scores = pipeline.decision_function(design_frame(frame))
    return np.column_stack([-scores, scores]) if scores.ndim == 1 else scores


def _stratified_split(y: np.ndarray, fraction: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Split indices, keeping class balance when the classes allow it.

    Stratification fails when some class has a single member. That is a normal
    state here — `fid` and `construction` are barely labelled — so fall back to
    an unstratified split rather than refusing to train.
    """
    indices = np.arange(len(y))
    _, counts = np.unique(y, return_counts=True)
    stratify = y if counts.min() >= 2 else None
    return train_test_split(indices, test_size=fraction, random_state=seed, stratify=stratify)


def _version(
    feature_names: list[str], classes: tuple[Stage, ...], y: np.ndarray, alpha: float, seed: int
) -> str:
    """Content hash of the training inputs.

    Two runs over the same data with the same settings produce the same version,
    so re-running training is idempotent rather than accumulating near-identical
    model rows.
    """
    payload = json.dumps(
        {
            "features": feature_names,
            "classes": [stage.value for stage in classes],
            "labels": hashlib.sha256(y.tobytes()).hexdigest(),
            "n": int(len(y)),
            "alpha": alpha,
            "seed": seed,
            "algo": ALGO,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def rank_metrics(y_true: np.ndarray, y_predicted: np.ndarray) -> dict[str, float]:
    """Ordinal error, which plain accuracy cannot see.

    Predicting FEED when the truth is FEL1 is a smaller mistake than predicting
    COD, and accuracy scores both as one wrong answer. Mean absolute rank error
    and quadratic-weighted kappa both take the ordering seriously.
    """
    if len(y_true) == 0:
        return {"rank_mae": float("nan"), "qwk": float("nan")}

    mae = float(np.abs(y_true.astype(float) - y_predicted.astype(float)).mean())

    # QWK is undefined when either side has a single class — there is no
    # disagreement structure to measure.
    if len(np.unique(y_true)) < 2 or len(np.unique(y_predicted)) < 2:
        qwk = float("nan")
    else:
        qwk = float(cohen_kappa_score(y_true, y_predicted, weights="quadratic"))

    return {"rank_mae": mae, "qwk": qwk}


def brier_score(probabilities: np.ndarray, y_true: np.ndarray, n_classes: int) -> float:
    """Multiclass Brier score: mean squared error against the one-hot truth."""
    if len(y_true) == 0:
        return float("nan")
    one_hot = np.zeros((len(y_true), n_classes), dtype=float)
    one_hot[np.arange(len(y_true)), y_true] = 1.0
    return float(((probabilities - one_hot) ** 2).sum(axis=1).mean())


def train(
    frame: pl.DataFrame,
    labels: list[WeakLabel],
    *,
    alpha: float = DEFAULT_ALPHA,
    seed: int = DEFAULT_SEED,
    notes: str = "",
) -> TrainedModel:
    """Fit and calibrate on usable weak labels.

    `frame` is the raw feature frame; `labels` is aligned to it by `inr`, not by
    position, so a caller may pass more labels than rows or the other way round
    without producing a quietly mislabelled model.
    """
    usable = {label.inr: label for label in labels if label.usable}
    if not usable:
        raise ValueError(
            "no usable weak labels — every label was withdrawn or below the "
            "confidence floor. Run `pipeline labels` to see the breakdown."
        )

    keep = frame.filter(pl.col("inr").is_in(list(usable)))
    if keep.height < 2:
        raise ValueError(f"only {keep.height} labelled rows survived the join to the feature frame")

    stages = [usable[inr].stage for inr in keep["inr"].to_list()]

    # Classes in lifecycle order, not sklearn's sorted-string order. The ordinal
    # metrics, the expected rank and the conformal expansion all assume that
    # class index i sits next to class index i+1 on the real lifecycle.
    classes = tuple(sorted(set(stages), key=lambda stage: stage.rank))
    index_of = {stage: index for index, stage in enumerate(classes)}
    y = np.array([index_of[stage] for stage in stages], dtype=int)

    small = keep.height < MIN_ROWS_FOR_CALIBRATION
    if small:
        train_index = np.arange(keep.height)
        calibration_index = test_index = np.array([], dtype=int)
    else:
        rest_index, test_index = _stratified_split(y, TEST_FRACTION, seed)
        relative_calibration = CALIBRATION_FRACTION / (1.0 - TEST_FRACTION)
        rest_train, rest_calibration = _stratified_split(y[rest_index], relative_calibration, seed)
        train_index = rest_index[rest_train]
        calibration_index = rest_index[rest_calibration]

        # A class that landed entirely outside the training split would make the
        # fitted classes disagree with `classes`. Fold back to train-only rather
        # than resampling, and say so in the metrics.
        if len(np.unique(y[train_index])) < len(classes):
            small = True
            train_index = np.arange(keep.height)
            calibration_index = test_index = np.array([], dtype=int)

    pipeline = Pipeline(
        [
            ("preprocess", make_preprocessor()),
            (
                "model",
                LogisticRegression(
                    max_iter=2000, class_weight="balanced", C=1.0, random_state=seed
                ),
            ),
        ]
    )
    pipeline.fit(design_frame(keep[train_index.tolist()]), y[train_index])

    model = TrainedModel(
        model_version="",
        pipeline=pipeline,
        classes=classes,
        n_train=int(len(train_index)),
        notes=notes,
    )

    metrics: dict[str, Any] = {
        "n_labelled": int(keep.height),
        "n_train": int(len(train_index)),
        "n_calibration": int(len(calibration_index)),
        "n_test": int(len(test_index)),
        "classes": [stage.value for stage in classes],
        "missing_classes": [stage.value for stage in Stage if stage not in index_of],
        "calibrated": not small,
    }

    if small:
        metrics["warning"] = (
            f"only {keep.height} labelled rows (or a class missing from the training "
            "split) — fitted without calibration. Probabilities are uncalibrated and "
            "no conformal interval is available."
        )
    else:
        calibration_frame = keep[calibration_index.tolist()]
        calibration_logits = _logits(pipeline, calibration_frame)
        model.temperature = fit_temperature(calibration_logits, y[calibration_index])

        calibration_probabilities = softmax(calibration_logits, model.temperature)
        model.conformal = fit_conformal(
            calibration_probabilities, y[calibration_index], alpha=alpha
        )

        metrics["temperature"] = model.temperature
        metrics["ece_uncalibrated"] = expected_calibration_error(
            softmax(calibration_logits, 1.0), y[calibration_index]
        )
        metrics["ece_calibrated"] = expected_calibration_error(
            calibration_probabilities, y[calibration_index]
        )

        test_frame = keep[test_index.tolist()]
        test_probabilities = model.probabilities(test_frame)
        y_test = y[test_index]
        predicted = test_probabilities.argmax(axis=1)

        metrics.update(rank_metrics(y_test, predicted))
        metrics["macro_f1"] = float(f1_score(y_test, predicted, average="macro", zero_division=0))
        metrics["per_class_f1"] = {
            stage.value: float(score)
            for stage, score in zip(
                classes,
                f1_score(
                    y_test,
                    predicted,
                    average=None,
                    labels=list(range(len(classes))),
                    zero_division=0,
                ),
                strict=True,
            )
        }
        metrics["accuracy"] = float((predicted == y_test).mean()) if len(y_test) else float("nan")
        metrics["brier"] = brier_score(test_probabilities, y_test, len(classes))
        metrics["log_loss"] = (
            float(log_loss(y_test, test_probabilities, labels=list(range(len(classes)))))
            if len(y_test)
            else float("nan")
        )
        metrics["ece"] = expected_calibration_error(test_probabilities, y_test)

        intervals = model.conformal.intervals(test_probabilities)
        covered = [
            low <= truth <= high for (low, high), truth in zip(intervals, y_test, strict=True)
        ]
        metrics["conformal"] = {
            "alpha": alpha,
            "steps": model.conformal.steps,
            "target_coverage": model.conformal.guaranteed_coverage,
            "empirical_coverage": float(np.mean(covered)) if covered else float("nan"),
            "mean_width": float(np.mean([high - low + 1 for low, high in intervals]))
            if intervals
            else float("nan"),
        }

        # Agreement with the rules that produced the labels. Reported, never
        # celebrated: high agreement mostly means the model learned the rules.
        metrics["rule_agreement"] = metrics["accuracy"]

    model.metrics = metrics
    model.model_version = _version(model.feature_names, classes, y, alpha, seed)
    return model


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------
def contributions(
    model: TrainedModel, row: np.ndarray, class_index: int, *, top: int = TOP_CONTRIBUTIONS
) -> list[dict[str, float]]:
    """The largest `coefficient x feature` terms behind one predicted class.

    Signed, so "no linked permit" showing up as a negative contribution is
    visible rather than being folded into an absolute magnitude.
    """
    terms = model.coefficients[class_index] * np.asarray(row, dtype=float)
    names = model.feature_names
    order = np.argsort(-np.abs(terms))[:top]
    return [
        {"feature": names[index], "value": float(row[index]), "contribution": float(terms[index])}
        for index in order
        if abs(terms[index]) > 1e-9
    ]


def predict_rows(
    model: TrainedModel,
    frame: pl.DataFrame,
    *,
    as_of: date,
    entity_ids: dict[str, str] | None = None,
    rule_labels: dict[str, WeakLabel] | None = None,
) -> list[dict[str, Any]]:
    """Prediction rows shaped for `stage_predictions`.

    `rule_labels` is optional and is stored beside the model's call rather than
    replacing it, so the two can be compared later. Where the rule fired on a
    date ERCOT actually reported the rule wins and `label_source` records that —
    a signed IA is a fact, not something to take a model's opinion on.

    That override used to key on `rule.confidence >= stages.MAX_CONFIDENCE`,
    which is unreachable: `_confidence` only touches 0.99 when all eleven
    `MILESTONE_FIELDS` are populated, and two of them (`construction_start`,
    `construction_end`) have no source in any table. Every prediction came back
    `label_source="model"`, including the several hundred sitting on a reported
    IA or energization date. `WeakLabel.decisive` names the condition directly
    instead of encoding it in a confidence threshold that cannot be met.
    """
    if frame.is_empty():
        return []

    entity_ids = entity_ids or {}
    rule_labels = rule_labels or {}

    matrix = model.design(frame)
    probabilities = model.probabilities(frame)
    summaries: list[Confidence] = summarize(probabilities)
    intervals = model.conformal.intervals(probabilities) if model.conformal else None

    rows: list[dict[str, Any]] = []

    for position, inr in enumerate(frame["inr"].to_list()):
        row_probabilities = probabilities[position]
        predicted_index = int(row_probabilities.argmax())
        summary = summaries[position]
        rule = rule_labels.get(inr)

        stage = model.classes[predicted_index]
        label_source = "model"
        if rule is not None and rule.decisive:
            stage = rule.stage
            label_source = "rule"

        interval = intervals[position] if intervals else None

        rows.append(
            {
                "entity_id": entity_ids.get(inr) or f"ercot:{inr}",
                "as_of": as_of,
                "model_version": model.model_version,
                "stage": stage.value,
                "label_source": label_source,
                "rule_stage": rule.stage.value if rule else None,
                # Handed over as objects, not as JSON text. The columns are
                # `jsonb`; a string here is stored as a JSON string scalar and
                # every reader downstream has to parse the payload a second time
                # to get at it.
                "probabilities": {
                    member.value: float(row_probabilities[index])
                    for index, member in enumerate(model.classes)
                },
                "confidence": summary.confidence,
                "margin": summary.margin,
                "entropy": summary.entropy,
                # Expressed on the full Stage scale rather than on the indices of
                # whichever classes survived training, so it stays comparable
                # across model versions.
                "expected_rank": float(
                    sum(
                        float(row_probabilities[index]) * member.rank
                        for index, member in enumerate(model.classes)
                    )
                ),
                "conformal_lo": model.classes[interval[0]].value if interval else None,
                "conformal_hi": model.classes[interval[1]].value if interval else None,
                "conformal_alpha": model.conformal.alpha if model.conformal else None,
                "withdrawn": bool(rule.withdrawn) if rule else False,
                "contributions": contributions(model, matrix[position], predicted_index),
                "justification": list(rule.justification) if rule else [],
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def to_model_run(model: TrainedModel, *, git_sha: str | None = None) -> dict[str, Any]:
    """The `model_runs` row for a trained model.

    Coefficients and feature names go to the database so a dashboard can explain
    a prediction from SQL alone. Reproducing one exactly means re-running
    training on the same inputs, which the content-hashed version makes
    verifiable.
    """
    return {
        "model_version": model.model_version,
        "algo": ALGO,
        "n_train": model.n_train,
        "n_features": len(model.feature_names),
        # Objects, not JSON text — see the note in `predict_rows`. `metrics` in
        # particular carries NaN whenever a metric is undefined; `store` strips
        # those recursively, which `json.dumps` here would have hidden behind an
        # unparseable `NaN` literal.
        "feature_names": model.feature_names,
        "classes": [stage.value for stage in model.classes],
        "temperature": model.temperature,
        "metrics": model.metrics,
        "coefficients": model.coefficients.tolist(),
        "intercepts": model.intercepts.tolist(),
        "git_sha": git_sha,
        "notes": model.notes or None,
    }
