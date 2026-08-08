"""Stage classification (Source #3 output).

Turns the two ingested sources into a lifecycle position per project.

    stages      the deterministic rule table, stdlib only
    labels      rules -> weak training labels, with coverage diagnostics
    momentum    change-over-time metrics from the snapshot series
    features    snapshot + momentum + permits -> one row per project
    confidence  temperature scaling and ordinal conformal intervals
    train       fit, calibrate, score, predict
    store       Supabase reads and writes
    service     read -> label -> train -> predict -> write

Nothing above `service` touches the database, and `stages` and `labels` import
no third-party package at all — the rules can be read and tested on their own.
"""
from .labels import WeakLabel, label_quality, weak_label
from .momentum import compute_momentum
from .service import (
    Context,
    NoDataError,
    load_context,
    run_classification,
    run_labels,
    run_momentum,
)
from .stages import Stage, StageInference, infer_stage

__all__ = [
    "Context",
    "NoDataError",
    "Stage",
    "StageInference",
    "WeakLabel",
    "compute_momentum",
    "infer_stage",
    "label_quality",
    "load_context",
    "run_classification",
    "run_labels",
    "run_momentum",
    "weak_label",
]
