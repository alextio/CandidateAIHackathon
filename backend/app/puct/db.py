"""Supabase persistence for PUCT filings, milestone events, and resolved links.

Reuses the shared config/get_client and the source-agnostic project_events and
resolved_links tables (no new event/link tables). Only puct_filings is new. The
resolved_links reuse maps PUCT identifiers onto the existing columns:
  source='puct', rn_number=<party slug>, permit_no=<control number>,
  inr=<matched ERCOT inr>; the PUCT party name goes in tceq_name (left side) and
  the matched ERCOT/TCEQ name in ercot_name — documented here since the column
  names were coined for Source #2.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from supabase import Client

from ..db import get_client
from ..models import FilingRecord, ProjectEvent
# Reuse Source #2's source-agnostic event upsert and shared table names.
from ..tceq.db import (
    ERCOT_TABLE,
    EVENTS_TABLE,
    LINKS_TABLE,
    load_ercot_projects,
    upsert_events,
)
from .resolve import PuctLinkResult

__all__ = [
    "get_client",
    "FILINGS_TABLE",
    "EVENTS_TABLE",
    "LINKS_TABLE",
    "ERCOT_TABLE",
    "TCEQ_TABLE",
    "upsert_filings",
    "upsert_events",
    "upsert_links",
    "load_ercot_projects",
    "load_tceq_companies",
]

FILINGS_TABLE = "puct_filings"
TCEQ_TABLE = "tceq_permits"


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _filing_row(rec: FilingRecord, now_iso: str) -> dict[str, Any]:
    return {
        "control_number": rec.control_number,
        "item_number": rec.item_number,
        "snapshot_date": _iso(rec.snapshot_date),
        "utility_type": rec.utility_type,
        "filed_date": _iso(rec.filed_date),
        "filing_party": rec.filing_party,
        "item_type": rec.item_type,
        "item_type_label": rec.item_type_label,
        "filing_description": rec.filing_description,
        "docket_title": rec.docket_title,
        "source_url": rec.source_url,
        "state": rec.state,
        "raw": rec.raw,
        "last_seen_at": now_iso,
    }


def _link_row(link: PuctLinkResult, now_iso: str) -> dict[str, Any]:
    return {
        "source": "puct",
        "rn_number": link.party_slug,       # PUCT side: normalized party name
        "permit_no": link.control_number,   # PUCT side: docket/control number
        "inr": link.inr,                    # matched ERCOT project (null if TCEQ/none)
        "status": link.status,
        "score": link.score,
        "method": link.method,
        "tceq_name": link.party,            # reused column: the PUCT party name
        "ercot_name": link.matched_name,    # reused column: matched ERCOT/TCEQ name
        "county": None,                     # PUCT filings carry no county
        "raw": {
            **link.raw,
            "matched_source": link.matched_source,
            "tceq_rn": link.tceq_rn,
            "control_number": link.control_number,
            "party": link.party,
        },
        "last_seen_at": now_iso,
    }


def _upsert(client: Client, table: str, rows: list[dict], on_conflict: str, batch_size: int) -> int:
    written = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        client.table(table).upsert(chunk, on_conflict=on_conflict).execute()
        written += len(chunk)
    return written


def upsert_filings(client: Client, records: list[FilingRecord], *, batch_size: int = 500) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    # Collapse to the composite PK so one batch has no duplicate key values.
    deduped: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for rec in records:
        row = _filing_row(rec, now_iso)
        deduped[(row["control_number"], row["item_number"], row["snapshot_date"])] = row
    rows = list(deduped.values())
    return _upsert(client, FILINGS_TABLE, rows, "control_number,item_number,snapshot_date", batch_size)


def upsert_links(client: Client, links: list[PuctLinkResult], *, batch_size: int = 500) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    # PK is (source, rn_number, permit_no); collapse duplicates within a batch.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for link in links:
        row = _link_row(link, now_iso)
        deduped[(row["rn_number"], row["permit_no"])] = row
    rows = list(deduped.values())
    return _upsert(client, LINKS_TABLE, rows, "source,rn_number,permit_no", batch_size)


def load_tceq_companies(client: Client, *, limit: int = 50000) -> list[dict]:
    """Load tceq_permits names to resolve PUCT parties against (latest snapshot).

    De-duped to the most recent snapshot per RN so churn across runs doesn't
    inflate the candidate index.
    """
    resp = (
        client.table(TCEQ_TABLE)
        .select("rn_number,company_name,entity_name,county,snapshot_date")
        .order("snapshot_date", desc=True)
        .limit(limit)
        .execute()
    )
    seen: dict[str, dict] = {}
    for row in resp.data or []:
        rn = row.get("rn_number")
        if rn and rn not in seen:  # first == newest
            seen[rn] = row
    return list(seen.values())
