"""Calibration and uncertainty — turning scores into numbers that mean something.

A softmax output is not a confidence. Logistic regression is provably
over-confident even when the model is correctly specified and data is plentiful
(Bai et al., "Don't Just Blame Over-parametrization for Over-confidence", 2021),
so the raw top probability systematically overstates how often the model is
right. Three pieces fix that, in order of increasing strength:

1. Temperature scaling — one scalar, fitted on held-out logits, that stretches
   or shrinks the whole distribution. It cannot change which class wins, so
   accuracy is untouched and only the probabilities move.

2. A confidence triple, because "how sure are you" is three questions:
       confidence  how likely is the top class            max p
       margin      how much better than the runner-up     p1 - p2
       entropy     how spread out is the whole belief     normalized 0..1
   A prediction with confidence 0.5 and margin 0.45 is decisive; one with
   confidence 0.5 and margin 0.02 is a coin flip between two stages. Reporting
   only the first cannot distinguish them.

3. Split conformal prediction, which gives an actual guarantee rather than a
   calibrated guess: the returned set contains the true stage at least (1-alpha)
   of the time, over the calibration distribution.

The conformal sets here are ordinal, meaning contiguous. Standard APS (Romano,
Sesia & Candes, NeurIPS 2020) can return {concept, fid} — a set that is valid
but reads as nonsense on a dashboard. Since stages are ordered, expanding to
neighbours keeps every set an interval: "between FEED and FID" is a sentence a
non-technical user can act on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize_scalar

# Bounds on temperature. Below 1 the model is under-confident and is being
# sharpened; above 1 it is being softened. Values outside this range mean
# something is wrong upstream rather than that the model needs extreme scaling.
MIN_TEMPERATURE = 0.05
MAX_TEMPERATURE = 20.0

DEFAULT_ALPHA = 0.10
DEFAULT_ECE_BINS = 10


def softmax(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    """Row-wise softmax at a given temperature, computed stably."""
    scaled = np.asarray(logits, dtype=float) / temperature
    scaled -= scaled.max(axis=1, keepdims=True)
    exponentiated = np.exp(scaled)
    return exponentiated / exponentiated.sum(axis=1, keepdims=True)


def fit_temperature(logits: np.ndarray, y_true: np.ndarray) -> float:
    """Find the single scalar T minimising negative log likelihood.

    Must be fitted on data the model did not train on. Fitting it on training
    logits returns roughly 1.0 and calibrates nothing, because the model is
    already over-fit to exactly those points.
    """
    logits = np.asarray(logits, dtype=float)
    y_true = np.asarray(y_true, dtype=int)

    if logits.ndim != 2:
        raise ValueError(f"expected 2-D logits, got shape {logits.shape}")
    if len(logits) != len(y_true):
        raise ValueError(f"{len(logits)} logit rows against {len(y_true)} labels")
    if len(logits) == 0:
        raise ValueError("cannot fit temperature on an empty calibration set")

    rows = np.arange(len(y_true))

    def negative_log_likelihood(log_temperature: float) -> float:
        probabilities = softmax(logits, math.exp(log_temperature))
        # Clip before the log: a confidently wrong prediction can otherwise
        # produce -inf and take the whole optimisation with it.
        return float(-np.log(np.clip(probabilities[rows, y_true], 1e-12, 1.0)).mean())

    result = minimize_scalar(
        negative_log_likelihood,
        bounds=(math.log(MIN_TEMPERATURE), math.log(MAX_TEMPERATURE)),
        method="bounded",
    )
    return float(np.clip(math.exp(result.x), MIN_TEMPERATURE, MAX_TEMPERATURE))


@dataclass(frozen=True)
class Confidence:
    """The three numbers stored for every prediction."""

    confidence: float
    margin: float
    entropy: float
    expected_rank: float


def summarize(probabilities: np.ndarray) -> list[Confidence]:
    """Confidence triple plus expected rank, per row."""
    probabilities = np.atleast_2d(np.asarray(probabilities, dtype=float))
    n_classes = probabilities.shape[1]

    ordered = np.sort(probabilities, axis=1)[:, ::-1]
    top = ordered[:, 0]
    runner_up = ordered[:, 1] if n_classes > 1 else np.zeros_like(top)

    with np.errstate(divide="ignore", invalid="ignore"):
        raw_entropy = -(probabilities * np.log(np.clip(probabilities, 1e-12, 1.0))).sum(axis=1)
    # Normalised by log(K) so entropy is 0..1 regardless of how many classes
    # survived training — otherwise the number is not comparable across models.
    normalized = raw_entropy / math.log(n_classes) if n_classes > 1 else np.zeros_like(raw_entropy)

    ranks = np.arange(n_classes)
    expected = (probabilities * ranks).sum(axis=1)

    return [
        Confidence(
            confidence=float(top[index]),
            margin=float(top[index] - runner_up[index]),
            entropy=float(normalized[index]),
            expected_rank=float(expected[index]),
        )
        for index in range(len(probabilities))
    ]


# ---------------------------------------------------------------------------
# Ordinal conformal prediction
# ---------------------------------------------------------------------------
def expansion_order(probabilities: np.ndarray) -> list[int]:
    """Order in which classes join the interval, starting from the mode.

    Begin at the most likely class, then repeatedly absorb whichever immediate
    neighbour — one below or one above — carries more probability. The result is
    always contiguous, which is the property that makes the eventual set
    readable as a stage range.
    """
    probabilities = np.asarray(probabilities, dtype=float).ravel()
    n_classes = len(probabilities)

    start = int(np.argmax(probabilities))
    order = [start]
    low = high = start

    while len(order) < n_classes:
        below = probabilities[low - 1] if low > 0 else -np.inf
        above = probabilities[high + 1] if high < n_classes - 1 else -np.inf

        if below >= above:
            low -= 1
            order.append(low)
        else:
            high += 1
            order.append(high)

    return order


def expansion_rank(probabilities: np.ndarray, label: int) -> int:
    """How many expansion steps are needed before `label` is inside the interval.

    This is the conformal nonconformity score: 0 when the model's top class is
    correct, larger the further the truth sits from where the model is looking.
    """
    return expansion_order(probabilities).index(int(label))


@dataclass(frozen=True)
class ConformalCalibrator:
    """A fitted split-conformal rule producing contiguous stage intervals."""

    steps: int
    alpha: float
    n_calibration: int
    n_classes: int

    def interval(self, probabilities: np.ndarray) -> tuple[int, int]:
        """Lowest and highest class index in the prediction set."""
        order = expansion_order(probabilities)
        included = order[: self.steps + 1]
        return min(included), max(included)

    def intervals(self, probabilities: np.ndarray) -> list[tuple[int, int]]:
        return [self.interval(row) for row in np.atleast_2d(probabilities)]

    @property
    def guaranteed_coverage(self) -> float:
        return 1.0 - self.alpha


def fit_conformal(
    probabilities: np.ndarray,
    y_true: np.ndarray,
    *,
    alpha: float = DEFAULT_ALPHA,
) -> ConformalCalibrator:
    """Calibrate the interval width that achieves 1-alpha coverage.

    Must use held-out data, and data exchangeable with what will be predicted.
    The guarantee is marginal — it holds on average over the population, not
    separately within every stage — so a rare stage can still be under-covered
    while the overall number looks right.
    """
    probabilities = np.atleast_2d(np.asarray(probabilities, dtype=float))
    y_true = np.asarray(y_true, dtype=int)

    n_calibration = len(y_true)
    if n_calibration == 0:
        raise ValueError("cannot calibrate conformal intervals on an empty set")
    if len(probabilities) != n_calibration:
        raise ValueError(f"{len(probabilities)} rows against {n_calibration} labels")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must lie in (0, 1), got {alpha}")

    scores = np.array(
        [expansion_rank(probabilities[index], y_true[index]) for index in range(n_calibration)]
    )

    # The finite-sample correction: the (n+1)(1-alpha) order statistic, not the
    # plain empirical quantile. Without the +1 the guarantee does not hold on
    # small calibration sets, which is exactly where it matters.
    position = math.ceil((n_calibration + 1) * (1.0 - alpha))
    position = min(position, n_calibration)
    steps = int(np.sort(scores)[position - 1])

    return ConformalCalibrator(
        steps=steps,
        alpha=alpha,
        n_calibration=n_calibration,
        n_classes=probabilities.shape[1],
    )


# ---------------------------------------------------------------------------
# Calibration metrics
# ---------------------------------------------------------------------------
def expected_calibration_error(
    probabilities: np.ndarray,
    y_true: np.ndarray,
    *,
    n_bins: int = DEFAULT_ECE_BINS,
) -> float:
    """Mean gap between stated confidence and observed accuracy.

    Predictions are bucketed by their top probability; within each bucket the
    model claims some average confidence and achieves some accuracy. ECE is the
    size-weighted average distance between the two. Zero is perfect; a
    well-behaved model after temperature scaling should land under about 0.05.
    """
    probabilities = np.atleast_2d(np.asarray(probabilities, dtype=float))
    y_true = np.asarray(y_true, dtype=int)

    if len(y_true) == 0:
        return float("nan")

    confidence = probabilities.max(axis=1)
    predicted = probabilities.argmax(axis=1)
    correct = (predicted == y_true).astype(float)

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    total = 0.0

    for index in range(n_bins):
        low, high = edges[index], edges[index + 1]
        # Upper-inclusive on the last bin so confidence exactly 1.0 is counted.
        in_bin = (confidence > low) & (confidence <= high) if index else (confidence <= high)
        if not in_bin.any():
            continue
        weight = in_bin.mean()
        total += weight * abs(correct[in_bin].mean() - confidence[in_bin].mean())

    return float(total)


def reliability_bins(
    probabilities: np.ndarray,
    y_true: np.ndarray,
    *,
    n_bins: int = DEFAULT_ECE_BINS,
) -> list[dict[str, float]]:
    """Per-bin confidence against accuracy — the reliability diagram, as data."""
    probabilities = np.atleast_2d(np.asarray(probabilities, dtype=float))
    y_true = np.asarray(y_true, dtype=int)

    confidence = probabilities.max(axis=1)
    correct = (probabilities.argmax(axis=1) == y_true).astype(float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)

    bins = []
    for index in range(n_bins):
        low, high = edges[index], edges[index + 1]
        in_bin = (confidence > low) & (confidence <= high) if index else (confidence <= high)
        if not in_bin.any():
            continue
        bins.append(
            {
                "bin_low": float(low),
                "bin_high": float(high),
                "n": int(in_bin.sum()),
                "mean_confidence": float(confidence[in_bin].mean()),
                "accuracy": float(correct[in_bin].mean()),
            }
        )
    return bins


def risk_coverage_curve(
    probabilities: np.ndarray,
    y_true: np.ndarray,
    *,
    n_points: int = 20,
) -> list[dict[str, float]]:
    """Accuracy as a function of how many predictions you are willing to keep.

    Answers the question the dashboard actually needs: "if I only show calls
    above threshold t, how many projects do I show and how often am I right?"
    Selective prediction is usually worth far more than a marginal accuracy
    gain — a short, trustworthy list beats a long, noisy one.
    """
    probabilities = np.atleast_2d(np.asarray(probabilities, dtype=float))
    y_true = np.asarray(y_true, dtype=int)

    if len(y_true) == 0:
        return []

    confidence = probabilities.max(axis=1)
    correct = (probabilities.argmax(axis=1) == y_true).astype(float)

    curve = []
    for threshold in np.linspace(0.0, confidence.max(), n_points):
        kept = confidence >= threshold
        if not kept.any():
            continue
        curve.append(
            {
                "threshold": float(threshold),
                "coverage": float(kept.mean()),
                "n_kept": int(kept.sum()),
                "accuracy": float(correct[kept].mean()),
            }
        )
    return curve
