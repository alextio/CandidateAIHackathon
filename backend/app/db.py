"""Supabase persistence for discovered projects.

Configure with SUPABASE_URL + SUPABASE_SERVICE_KEY (any project, any account).
Projects are upserted on their natural key `inr`; `first_seen_at` is preserved
and `last_seen_at` bumped on every run so you can see churn across reports.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from supabase import Client, create_client

from .config import Settings
from .models import Project

TABLE = "ercot_projects"


def get_client(settings: Settings) -> Client:
    if not settings.supabase_configured:
        raise RuntimeError(
            "Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY."
        )
    return create_client(settings.supabase_url, settings.supabase_service_key)


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _row(project: Project, now_iso: str) -> dict[str, Any]:
    return {
        "inr": project.inr,
        "project_name": project.project_name,
        "size_category": project.size_category,
        "status": project.status,
        "interconnecting_entity": project.interconnecting_entity,
        "poi_location": project.poi_location,
        "county": project.county,
        "state": project.state,
        "cdr_reporting_zone": project.cdr_reporting_zone,
        "fuel": project.fuel,
        "fuel_label": project.fuel_label,
        "technology": project.technology,
        "technology_label": project.technology_label,
        "capacity_mw": project.capacity_mw,
        "gim_study_phase": project.gim_study_phase,
        "projected_cod": _iso(project.projected_cod),
        "ia_signed": project.ia_signed,
        "approved_for_energization": _iso(project.approved_for_energization),
        "approved_for_synchronization": _iso(project.approved_for_synchronization),
        "inactive_date": _iso(project.inactive_date),
        "cancel_date": _iso(project.cancel_date),
        "comment": project.comment,
        "report_date": _iso(project.report_date),
        "source_sheet": project.source_sheet,
        "source_file": project.source_file,
        "raw": project.raw,
        "last_seen_at": now_iso,
    }


def upsert_projects(
    client: Client, projects: list[Project], *, batch_size: int = 500
) -> int:
    """Upsert projects keyed on `inr`. Returns the number of rows written."""
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [_row(p, now_iso) for p in projects]
    written = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        client.table(TABLE).upsert(chunk, on_conflict="inr,report_date").execute()
        written += len(chunk)
    return written
