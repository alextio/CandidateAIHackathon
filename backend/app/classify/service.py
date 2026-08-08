"""Orchestration: read -> label -> momentum -> train -> predict -> write.

One module so the endpoints stay thin, and so training and scoring cannot drift
apart. Both go through `load_context`, which is the classic way a model ends up
working offline and not live: a feature built one way for training and another
way for inference.

Training and scoring happen in the same call. The model exists for the duration
of the request and is never written to disk — see the note in `train.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl
from supabase import Client

from . import store
from .features import build_feature_frame, snapshots_frame
from .labels import WeakLabel, label_quality, weak_label
from .momentum import MOMENTUM_COLUMNS, compute_momentum
from .train import TrainedModel, predict_rows, to_model_run, train


class NoDataError(RuntimeError):
    """Nothing to classify. The message is meant for a human, not a stack trace."""


@dataclass
class Context:
    """Everything both training and scoring read, loaded once."""

    as_of: date
    latest: list[dict[str, Any]]
    permits: dict[str, list[dict[str, Any]]]
    momentum: pl.DataFrame
    frame: pl.DataFrame
    labels: list[WeakLabel]


def entity_id_for(inr: str) -> str:
    """Stable identity for an ERCOT project.

    Prefixed so that if a cross-source resolver ever assigns real cluster ids,
    the backfill is a visible change rather than a silent re-key of predictions
    already stored.
    """
    return f"ercot:{inr}"


def load_context(client: Client, as_of: date | None = None) -> Context:
    resolved = as_of or store.latest_report_date(client)
    if resolved is None:
        raise NoDataError(
            "ercot_projects is empty — run POST /discover/ercot before classifying"
        )

    history = store.fetch_snapshots(client, as_of=resolved)
    if not history:
        raise NoDataError(f"no ercot_projects snapshots at or before {resolved}")

    latest = store.latest_by_inr(history)
    permits = store.fetch_linked_permits(client, as_of=resolved)

    momentum = compute_momentum(snapshots_frame(history), resolved)
    frame = build_feature_frame(latest, momentum, as_of=resolved, linked_permits=permits)

    labels = [
        weak_label(
            row,
            entity_id=entity_id_for(str(row.get("inr") or "")),
            as_of=resolved,
            linked_permits=permits.get(str(row.get("inr") or ""), []),
        )
        for row in latest
    ]

    return Context(
        as_of=resolved,
        latest=latest,
        permits=permits,
        momentum=momentum,
        frame=frame,
        labels=labels,
    )


def momentum_rows(momentum: pl.DataFrame) -> list[dict[str, Any]]:
    """Momentum frame as `project_momentum` rows."""
    if momentum.is_empty():
        return []
    return momentum.select(MOMENTUM_COLUMNS).to_dicts()


def grade_counts(momentum: pl.DataFrame) -> dict[str, int]:
    if momentum.is_empty():
        return {}
    counts = momentum.group_by("momentum_grade").len().sort("len", descending=True)
    return {row["momentum_grade"]: row["len"] for row in counts.to_dicts()}


def stage_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["stage"]] = counts.get(row["stage"], 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def run_momentum(
    client: Client, *, as_of: date | None = None, persist: bool = True
) -> dict[str, Any]:
    """Compute the snapshot-series metrics, and store them unless asked not to."""
    context = load_context(client, as_of)
    rows = momentum_rows(context.momentum)

    written = store.upsert_momentum(client, rows) if persist else 0

    return {
        "as_of": context.as_of.isoformat(),
        "projects": context.momentum.height,
        "by_grade": grade_counts(context.momentum),
        "written": written,
        "persisted": persist,
    }


def run_labels(client: Client, *, as_of: date | None = None) -> dict[str, Any]:
    """Weak-label coverage, without training. Read-only."""
    context = load_context(client, as_of)
    quality = label_quality(context.labels)

    return {
        "as_of": context.as_of.isoformat(),
        "projects": len(context.latest),
        "linked_permits": sum(len(permits) for permits in context.permits.values()),
        "labels": quality.as_dict(),
    }


def run_classification(
    client: Client,
    *,
    as_of: date | None = None,
    alpha: float = 0.10,
    seed: int = 20260808,
    notes: str | None = None,
    persist: bool = True,
    include_momentum: bool = True,
) -> dict[str, Any]:
    """The whole path: label, fit, calibrate, score, write.

    `persist=False` runs everything and returns the summary without touching a
    table — the fast way to see whether the labels are good enough before
    writing a model run that someone will later have to explain.
    """
    context = load_context(client, as_of)
    quality = label_quality(context.labels)

    model: TrainedModel = train(
        context.frame,
        context.labels,
        alpha=alpha,
        seed=seed,
        notes=notes or "",
    )

    rows = predict_rows(
        model,
        context.frame,
        as_of=context.as_of,
        entity_ids={inr: entity_id_for(inr) for inr in context.frame["inr"].to_list()},
        rule_labels={label.inr: label for label in context.labels},
    )

    disagreements = sum(
        1 for row in rows if row["rule_stage"] and row["rule_stage"] != row["stage"]
    )

    written = {"model_runs": 0, "stage_predictions": 0, "project_momentum": 0}
    if persist:
        # The model run goes in first: stage_predictions.model_version is a
        # foreign key, so the reverse order fails on every row.
        written["model_runs"] = store.upsert_model_run(client, to_model_run(model))
        written["stage_predictions"] = store.upsert_predictions(client, rows)
        if include_momentum:
            written["project_momentum"] = store.upsert_momentum(
                client, momentum_rows(context.momentum)
            )

    return {
        "as_of": context.as_of.isoformat(),
        "model_version": model.model_version,
        "projects": len(rows),
        "labels": quality.as_dict(),
        "metrics": model.metrics,
        "by_stage": stage_counts(rows),
        "rule_disagreements": disagreements,
        "written": written,
        "persisted": persist,
    }
