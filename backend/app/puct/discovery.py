"""PUCT discovery orchestration: fetch -> parse -> events -> resolve -> persist.

Pulls the curated seed dockets (or a caller-supplied list) from the PUCT
Interchange, normalizes their filings into records + milestone events, optionally
resolves the filing parties to ERCOT/TCEQ entities, and optionally persists
everything. Each run is a single snapshot keyed on the run date, mirroring
Sources #1 and #2.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import Settings
from ..models import FilingRecord, ProjectEvent
from . import codes, db, interchange, parser
from .resolve import PuctLinkResult, resolve_records


@dataclass
class DocketSummary:
    control_number: str
    label: str | None
    filings: int
    events: int


@dataclass
class PuctDiscoveryResult:
    snapshot_date: str
    dockets_requested: list[str]
    total_filings: int = 0
    total_events: int = 0
    events_by_type: dict[str, int] = field(default_factory=dict)
    parties_resolved: int = 0
    parties_review: int = 0
    parties_unresolved: int = 0
    links_to_ercot: int = 0
    links_to_tceq: int = 0
    filings_persisted: int = 0
    events_persisted: int = 0
    links_persisted: int = 0
    persisted_to_supabase: bool = False
    dockets: list[DocketSummary] = field(default_factory=list)


def discover(
    settings: Settings,
    *,
    dockets: list[str] | None = None,
    entity_match: bool = True,
    persist: bool = True,
    resolve: bool = True,
) -> tuple[PuctDiscoveryResult, list[FilingRecord], list[ProjectEvent], list[PuctLinkResult]]:
    """Run the PUCT pipeline and return the summary plus the produced objects."""
    control_numbers = dockets or codes.SEED_CONTROL_NUMBERS
    snapshot_date = datetime.now(timezone.utc).date()
    result = PuctDiscoveryResult(
        snapshot_date=snapshot_date.isoformat(),
        dockets_requested=list(control_numbers),
    )

    all_records: list[FilingRecord] = []
    all_events: list[ProjectEvent] = []
    with interchange.make_client() as client:
        docket_batches = interchange.fetch_dockets(client, control_numbers)
        for batch in docket_batches:
            records = parser.parse_docket(batch, snapshot_date=snapshot_date)
            events = parser.derive_all_events(records)
            all_records.extend(records)
            all_events.extend(events)
            result.dockets.append(
                DocketSummary(
                    control_number=batch.control_number,
                    label=codes.SEED_LABELS.get(batch.control_number) or batch.docket_title,
                    filings=len(records),
                    events=len(events),
                )
            )

    result.total_filings = len(all_records)
    result.total_events = len(all_events)
    result.events_by_type = dict(Counter(e.event_type for e in all_events))

    links: list[PuctLinkResult] = []
    supabase = None
    if (resolve or persist) and settings.supabase_configured:
        supabase = db.get_client(settings)

    if resolve and entity_match and supabase is not None:
        ercot_rows = db.load_ercot_projects(supabase)
        tceq_rows = db.load_tceq_companies(supabase)
        links = resolve_records(all_records, ercot_rows, tceq_rows)
        status_counts = Counter(link.status for link in links)
        result.parties_resolved = status_counts.get("resolved", 0)
        result.parties_review = status_counts.get("review", 0)
        result.parties_unresolved = status_counts.get("unresolved", 0)
        result.links_to_ercot = sum(1 for link in links if link.matched_source == "ercot")
        result.links_to_tceq = sum(1 for link in links if link.matched_source == "tceq")

    if persist and supabase is not None:
        result.filings_persisted = db.upsert_filings(supabase, all_records)
        result.events_persisted = db.upsert_events(supabase, all_events)
        if links:
            result.links_persisted = db.upsert_links(supabase, links)
        result.persisted_to_supabase = True

    return result, all_records, all_events, links
