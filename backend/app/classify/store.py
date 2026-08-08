"""Supabase reads and writes for the classifier.

Goes through `supabase-py` like the rest of the backend rather than opening a
Postgres connection, so the classifier needs no credential the app does not
already have.

Two consequences worth knowing:

* PostgREST has no `distinct on`, so "newest snapshot per project" is done in
  Python by `latest_by_inr`. That costs nothing here — momentum needs the full
  history anyway, so the rows are already in memory.
* PostgREST caps a response at 1,000 rows. Every read below pages explicitly.
  A silent truncation at 1,000 would produce a model trained on the alphabetical
  first tenth of the queue, which is the kind of bug that looks like a bad model
  rather than a bad query.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from supabase import Client

PAGE_SIZE = 1000
UPSERT_BATCH = 500

ERCOT_TABLE = "ercot_projects"
PERMITS_TABLE = "tceq_permits"
LINKS_TABLE = "resolved_links"
MOMENTUM_TABLE = "project_momentum"
PREDICTIONS_TABLE = "stage_predictions"
MODEL_RUNS_TABLE = "model_runs"

# resolved_links.status values worth treating as a real link. 'review' is
# included on purpose: a permit that is probably this project's is better
# evidence for the FID rule than no permit at all, and the rule itself is
# conservative about what it fires on.
LINKED_STATUSES = ("resolved", "review")

# Explicit rather than `select("*")` so a new column upstream cannot silently
# change the feature frame's schema.
SNAPSHOT_COLUMNS = (
    "inr",
    "report_date",
    "project_name",
    "size_category",
    "status",
    "interconnecting_entity",
    "county",
    "state",
    "cdr_reporting_zone",
    "fuel",
    "fuel_label",
    "technology",
    "technology_label",
    "capacity_mw",
    "gim_study_phase",
    "projected_cod",
    "ia_signed",
    "approved_for_energization",
    "approved_for_synchronization",
    "inactive_date",
    "cancel_date",
    "source_sheet",
)

PERMIT_COLUMNS = (
    "rn_number",
    "permit_no",
    "snapshot_date",
    "entity_name",
    "company_name",
    "county",
    "naics_code",
    "on_thesis",
    "entity_status",
    "affiliation_begin_date",
    "affiliation_end_date",
)


def _iso(value: Any) -> Any:
    """Dates to ISO strings; everything else through untouched."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _json_safe(value: Any) -> Any:
    """NaN and infinity become null.

    Momentum emits NaN for projects whose slope could not be estimated. JSON has
    no NaN literal, and PostgREST rejects the row rather than coercing it, so a
    single unestimable project would fail the whole batch.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return _iso(value)


def _rows(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(value) for key, value in payload.items()}


def _paged(query_factory, *, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """Run a select in pages until a short page comes back."""
    collected: list[dict[str, Any]] = []
    start = 0

    while True:
        response = query_factory().range(start, start + page_size - 1).execute()
        batch = response.data or []
        collected.extend(batch)
        if len(batch) < page_size:
            return collected
        start += page_size


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def fetch_snapshots(client: Client, *, as_of: date | None = None) -> list[dict[str, Any]]:
    """Full snapshot history, optionally truncated at `as_of`.

    The `as_of` filter is the leakage guard. Applied server-side, so a run
    reconstructing what was knowable in March does not pull April's rows across
    the wire at all.
    """

    def query():
        builder = client.table(ERCOT_TABLE).select(",".join(SNAPSHOT_COLUMNS))
        if as_of is not None:
            builder = builder.lte("report_date", as_of.isoformat())
        return builder.order("inr").order("report_date")

    return _paged(query)


def latest_by_inr(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per project: the newest snapshot present.

    Compares ISO date strings, which sort correctly, so this does not depend on
    whether PostgREST handed back `date` objects or text.
    """
    newest: dict[str, dict[str, Any]] = {}

    for row in snapshots:
        inr = str(row.get("inr") or "")
        if not inr:
            continue
        current = newest.get(inr)
        if current is None or str(row.get("report_date") or "") >= str(
            current.get("report_date") or ""
        ):
            newest[inr] = row

    return list(newest.values())


def latest_report_date(client: Client) -> date | None:
    """Newest snapshot month in the queue, or None when nothing is ingested."""
    response = (
        client.table(ERCOT_TABLE)
        .select("report_date")
        .order("report_date", desc=True)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    if not rows:
        return None
    value = rows[0]["report_date"]
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def fetch_linked_permits(
    client: Client, *, as_of: date | None = None
) -> dict[str, list[dict[str, Any]]]:
    """TCEQ permits linked to each project, keyed by `inr`.

    Two round trips rather than a join, because PostgREST can only embed across
    a declared foreign key and `resolved_links` has none to `tceq_permits`.
    Only the newest snapshot of each permit is kept — older snapshots of one
    permit say nothing extra about whether that permit exists.
    """
    links = _paged(
        lambda: client.table(LINKS_TABLE)
        .select("inr,rn_number,permit_no,status,score")
        .in_("status", list(LINKED_STATUSES))
        .not_.is_("inr", "null")
        .order("rn_number")
    )
    if not links:
        return {}

    rn_numbers = sorted({link["rn_number"] for link in links})

    permits: list[dict[str, Any]] = []
    for start in range(0, len(rn_numbers), UPSERT_BATCH):
        chunk = rn_numbers[start : start + UPSERT_BATCH]

        def query(chunk=chunk):
            builder = (
                client.table(PERMITS_TABLE)
                .select(",".join(PERMIT_COLUMNS))
                .in_("rn_number", chunk)
            )
            if as_of is not None:
                builder = builder.lte("snapshot_date", as_of.isoformat())
            return builder.order("rn_number").order("permit_no").order("snapshot_date")

        permits.extend(_paged(query))

    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for permit in permits:
        key = (permit["rn_number"], permit.get("permit_no") or "")
        current = newest.get(key)
        if current is None or str(permit.get("snapshot_date") or "") >= str(
            current.get("snapshot_date") or ""
        ):
            newest[key] = permit

    linked: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        permit = newest.get((link["rn_number"], link.get("permit_no") or ""))
        if permit is None:
            continue
        linked.setdefault(link["inr"], []).append({**permit, "link_score": link.get("score")})

    return linked


def fetch_model_run(client: Client, model_version: str | None = None) -> dict[str, Any] | None:
    """One registered model — the named version, or the most recently trained."""
    builder = client.table(MODEL_RUNS_TABLE).select("*")
    if model_version:
        builder = builder.eq("model_version", model_version)
    else:
        builder = builder.order("trained_at", desc=True)

    rows = builder.limit(1).execute().data or []
    return rows[0] if rows else None


def fetch_predictions(
    client: Client,
    *,
    inr: str | None = None,
    stage: str | None = None,
    as_of: date | None = None,
    model_version: str | None = None,
    min_confidence: float | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Stored predictions, newest first.

    `inr` filters on the synthetic entity id this pipeline writes
    (`ercot:<inr>`), which is what every ERCOT-only project gets.
    """
    builder = client.table(PREDICTIONS_TABLE).select("*")

    if inr:
        builder = builder.eq("entity_id", f"ercot:{inr}")
    if stage:
        builder = builder.eq("stage", stage)
    if as_of is not None:
        builder = builder.eq("as_of", as_of.isoformat())
    if model_version:
        builder = builder.eq("model_version", model_version)
    if min_confidence is not None:
        builder = builder.gte("confidence", min_confidence)

    return (
        builder.order("as_of", desc=True)
        .order("confidence", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
        .data
        or []
    )


def fetch_momentum(
    client: Client,
    *,
    inr: str | None = None,
    grade: str | None = None,
    as_of: date | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    builder = client.table(MOMENTUM_TABLE).select("*")

    if inr:
        builder = builder.eq("inr", inr)
    if grade:
        builder = builder.eq("momentum_grade", grade)
    if as_of is not None:
        builder = builder.eq("as_of", as_of.isoformat())

    return (
        builder.order("as_of", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
        .data
        or []
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _upsert(client: Client, table: str, rows: list[dict[str, Any]], *, on_conflict: str) -> int:
    if not rows:
        return 0

    payload = [_rows(row) for row in rows]
    for start in range(0, len(payload), UPSERT_BATCH):
        chunk = payload[start : start + UPSERT_BATCH]
        client.table(table).upsert(chunk, on_conflict=on_conflict).execute()

    return len(payload)


def upsert_momentum(client: Client, rows: list[dict[str, Any]]) -> int:
    return _upsert(client, MOMENTUM_TABLE, rows, on_conflict="inr,as_of")


def upsert_predictions(client: Client, rows: list[dict[str, Any]]) -> int:
    return _upsert(
        client, PREDICTIONS_TABLE, rows, on_conflict="entity_id,as_of,model_version"
    )


def upsert_model_run(client: Client, row: dict[str, Any]) -> int:
    """Register a trained model. Re-registering a version overwrites it.

    Overwrite rather than reject because a version string is a content hash of
    the training inputs — the same version genuinely is the same model, and a
    re-run that reproduces it should not fail on a primary key.
    """
    return _upsert(client, MODEL_RUNS_TABLE, [row], on_conflict="model_version")
