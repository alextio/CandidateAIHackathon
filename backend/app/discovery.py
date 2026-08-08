"""Discovery orchestration: fetch recent GIS reports -> parse -> (optionally) persist.

Pulls the last N monthly GIS reports (default 12) and stores each as a monthly
snapshot, so downstream can track how the interconnection queue evolves.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import httpx

from . import db
from .config import Settings
from .ercot import codes, gis_report, parser
from .models import Project


@dataclass
class MonthlySnapshot:
    report_date: str | None
    source_file: str
    posted_date: str
    total: int
    total_capacity_mw: float
    by_technology: dict[str, int] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    months_requested: int
    reports_ingested: int
    report_dates: list[str] = field(default_factory=list)
    total_rows: int = 0
    persisted: int = 0
    persisted_to_supabase: bool = False
    snapshots: list[MonthlySnapshot] = field(default_factory=list)


def _snapshot(report_date, doc, projects: list[Project]) -> MonthlySnapshot:
    return MonthlySnapshot(
        report_date=report_date.isoformat() if report_date else None,
        source_file=doc.file_name,
        posted_date=doc.posted_date.isoformat(),
        total=len(projects),
        total_capacity_mw=round(
            sum(p.capacity_mw for p in projects if p.capacity_mw is not None), 2
        ),
        by_technology=dict(Counter((p.technology or "").upper() for p in projects)),
    )


def discover(
    settings: Settings,
    *,
    persist: bool = True,
    months: int = 12,
    technologies: set[str] | None = codes.DATA_CENTER_TECHNOLOGIES,
) -> tuple[DiscoveryResult, list[Project]]:
    """Fetch the last `months` GIS reports and normalize them into projects.

    Each report is kept as its own monthly snapshot (one row per project per
    report month). By default results are narrowed to the data-center-relevant
    technologies (CC, GT, IC, BA, EN, FC); pass ``technologies=None`` for all.
    """
    all_projects: list[Project] = []
    result = DiscoveryResult(months_requested=months, reports_ingested=0)

    with httpx.Client(follow_redirects=True) as client:
        docs = gis_report.recent_gis_documents(client, months=months)
        for doc in docs:
            # report_date is part of the primary key, so it must never be null;
            # fall back to the posting date if the file name can't be parsed.
            report_date = (
                gis_report.report_date_from_name(doc.file_name) or doc.posted_date
            )
            content = gis_report.download_document(client, doc)
            projects = parser.parse_workbook(
                content,
                report_date=report_date,
                source_file=doc.file_name,
                technologies=technologies,
            )
            all_projects.extend(projects)
            result.snapshots.append(_snapshot(report_date, doc, projects))
            result.reports_ingested += 1
            if report_date:
                result.report_dates.append(report_date.isoformat())

    result.total_rows = len(all_projects)

    if persist and settings.supabase_configured:
        supabase = db.get_client(settings)
        result.persisted = db.upsert_projects(supabase, all_projects)
        result.persisted_to_supabase = True

    return result, all_projects
