"""Supabase persistence for TCEQ permits, derived events, and resolved links.

Reuses the same SUPABASE_URL + SUPABASE_SERVICE_KEY config as Source #1 and the
shared get_client(). Upserts are keyed to match the migration's composite PKs.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from supabase import Client

from ..db import get_client  # re-exported for symmetry with the ercot module
from ..models import PermitRecord, ProjectEvent
from .resolve import LinkResult

__all__ = [
    "get_client",
    "PERMITS_TABLE",
    "EVENTS_TABLE",
    "LINKS_TABLE",
    "ERCOT_TABLE",
    "upsert_permits",
    "upsert_events",
    "upsert_links",
    "load_ercot_projects",
]

PERMITS_TABLE = "tceq_permits"
EVENTS_TABLE = "project_events"
LINKS_TABLE = "resolved_links"
ERCOT_TABLE = "ercot_projects"


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _permit_row(rec: PermitRecord, now_iso: str) -> dict[str, Any]:
    return {
        "rn_number": rec.rn_number,
        "permit_no": rec.permit_no or "",
        "snapshot_date": _iso(rec.snapshot_date),
        "program_code": rec.program_code,
        "entity_name": rec.entity_name,
        "company_name": rec.company_name,
        "company_legal_name": rec.company_legal_name,
        "county": rec.county,
        "city": rec.city,
        "address": rec.address,
        "state": rec.state,
        "naics_code": rec.naics_code,
        "naics_label": rec.naics_label,
        "on_thesis": rec.on_thesis,
        "entity_status": rec.entity_status,
        "company_status": rec.company_status,
        "latitude": rec.latitude,
        "longitude": rec.longitude,
        "geo_accuracy_m": rec.geo_accuracy_m,
        "geo_source": rec.geo_source,
        "geocoded_at": now_iso if rec.latitude is not None else None,
        "affiliation_begin_date": _iso(rec.affiliation_begin_date),
        "affiliation_end_date": _iso(rec.affiliation_end_date),
        "status_date": _iso(rec.status_date),
        "region": rec.region,
        "source_dataset": rec.source_dataset,
        "raw": rec.raw,
        "last_seen_at": now_iso,
    }


def _event_row(ev: ProjectEvent, now_iso: str) -> dict[str, Any]:
    return {
        "source": ev.source,
        "permit_no": ev.permit_no or "",
        "event_type": ev.event_type,
        "event_date": _iso(ev.event_date),
        "rn_number": ev.rn_number,
        "entity": ev.entity,
        "county": ev.county,
        "state": ev.state,
        "raw": ev.raw,
        "last_seen_at": now_iso,
    }


def _link_row(link: LinkResult, now_iso: str) -> dict[str, Any]:
    return {
        "source": "tceq",
        "rn_number": link.rn_number,
        "permit_no": link.permit_no or "",
        "inr": link.inr,
        "status": link.status,
        "score": link.score,
        "method": link.method,
        "tceq_name": link.tceq_name,
        "ercot_name": link.ercot_name,
        "county": link.county,
        "raw": link.raw,
        "last_seen_at": now_iso,
    }


def _upsert(client: Client, table: str, rows: list[dict], on_conflict: str, batch_size: int) -> int:
    written = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        client.table(table).upsert(chunk, on_conflict=on_conflict).execute()
        written += len(chunk)
    return written


def upsert_permits(client: Client, records: list[PermitRecord], *, batch_size: int = 500) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    # The feed can repeat a (rn_number, permit_no) within one snapshot; collapse
    # to the composite PK so a single upsert batch has no duplicate key values.
    deduped: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for rec in records:
        row = _permit_row(rec, now_iso)
        deduped[(row["rn_number"], row["permit_no"], row["snapshot_date"])] = row
    rows = list(deduped.values())
    return _upsert(client, PERMITS_TABLE, rows, "rn_number,permit_no,snapshot_date", batch_size)


def upsert_events(client: Client, events: list[ProjectEvent], *, batch_size: int = 500) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [_event_row(e, now_iso) for e in events]
    return _upsert(client, EVENTS_TABLE, rows, "source,permit_no,event_type,event_date", batch_size)


def upsert_links(client: Client, links: list[LinkResult], *, batch_size: int = 500) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [_link_row(link, now_iso) for link in links]
    return _upsert(client, LINKS_TABLE, rows, "source,rn_number,permit_no", batch_size)


def load_ercot_projects(client: Client, *, limit: int = 50000) -> list[dict]:
    """Load the ERCOT rows needed to resolve against (latest snapshot per project).

    We pull the columns entity resolution needs and de-dupe to the most recent
    report_date per INR so churn across monthly snapshots doesn't inflate the
    candidate index.
    """
    resp = (
        client.table(ERCOT_TABLE)
        .select("inr,project_name,interconnecting_entity,county,report_date")
        .order("report_date", desc=True)
        .limit(limit)
        .execute()
    )
    seen: dict[str, dict] = {}
    for row in resp.data or []:
        inr = row.get("inr")
        if inr and inr not in seen:  # first occurrence == newest (ordered desc)
            seen[inr] = row
    return list(seen.values())
