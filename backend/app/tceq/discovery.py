"""TCEQ discovery orchestration: fetch -> parse -> events -> resolve -> persist.

Pulls AIRNSR permit affiliations from the TCEQ Central Registry (all regions by
default), normalizes them into permit records + dated project events, links them
to ERCOT queue projects, and (optionally) persists everything. Each run is a
single snapshot keyed on the run date, mirroring Source #1's snapshot history.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx

from ..config import Settings
from ..models import PermitRecord, ProjectEvent
from . import central_registry, db, geocode, parser
from .resolve import LinkResult, resolve_records


@dataclass
class RegionSummary:
    region: str
    dataset_id: str
    fetched: int
    kept: int
    on_thesis: int


@dataclass
class DiscoveryResult:
    snapshot_date: str
    regions_requested: list[str]
    total_fetched: int = 0
    total_records: int = 0
    on_thesis_records: int = 0
    geocoded_records: int = 0
    total_events: int = 0
    events_by_type: dict[str, int] = field(default_factory=dict)
    links_resolved: int = 0
    links_review: int = 0
    links_unresolved: int = 0
    permits_persisted: int = 0
    events_persisted: int = 0
    links_persisted: int = 0
    persisted_to_supabase: bool = False
    regions: list[RegionSummary] = field(default_factory=list)


def discover(
    settings: Settings,
    *,
    persist: bool = True,
    regions: list[str] | None = None,
    on_thesis_only: bool = False,
    resolve: bool = True,
    geocode_permits: bool = True,
    max_rows_per_region: int | None = None,
) -> tuple[DiscoveryResult, list[PermitRecord], list[ProjectEvent], list[LinkResult]]:
    """Run the TCEQ pipeline and return the summary plus the produced objects."""
    snapshot_date = datetime.now(timezone.utc).date()
    result = DiscoveryResult(
        snapshot_date=snapshot_date.isoformat(),
        regions_requested=regions or list(central_registry.REGION_DATASETS),
    )

    all_records: list[PermitRecord] = []
    with httpx.Client(follow_redirects=True) as client:
        region_batches = central_registry.fetch_air_nsr(
            client, regions=regions, max_rows_per_region=max_rows_per_region
        )
        for batch in region_batches:
            records = parser.parse_rows(
                batch.rows,
                region=batch.region,
                source_dataset=batch.dataset_id,
                snapshot_date=snapshot_date,
                on_thesis_only=on_thesis_only,
            )
            all_records.extend(records)
            result.regions.append(
                RegionSummary(
                    region=batch.region,
                    dataset_id=batch.dataset_id,
                    fetched=len(batch.rows),
                    kept=len(records),
                    on_thesis=sum(1 for r in records if r.on_thesis),
                )
            )
            result.total_fetched += len(batch.rows)

        if geocode_permits:
            result.geocoded_records = geocode.geocode_records(client, all_records)

    result.total_records = len(all_records)
    result.on_thesis_records = sum(1 for r in all_records if r.on_thesis)

    events = parser.derive_all_events(all_records)
    result.total_events = len(events)
    result.events_by_type = dict(Counter(e.event_type for e in events))

    links: list[LinkResult] = []
    supabase = None
    if (resolve or persist) and settings.supabase_configured:
        supabase = db.get_client(settings)

    if resolve and supabase is not None:
        ercot_rows = db.load_ercot_projects(supabase)
        links = resolve_records(all_records, ercot_rows)
        status_counts = Counter(link.status for link in links)
        result.links_resolved = status_counts.get("resolved", 0)
        result.links_review = status_counts.get("review", 0)
        result.links_unresolved = status_counts.get("unresolved", 0)

    if persist and supabase is not None:
        result.permits_persisted = db.upsert_permits(supabase, all_records)
        result.events_persisted = db.upsert_events(supabase, events)
        if links:
            result.links_persisted = db.upsert_links(supabase, links)
        result.persisted_to_supabase = True

    return result, all_records, events, links
