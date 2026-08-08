"""Normalize scraped PUCT filing rows into FilingRecord + milestone ProjectEvents.

Each Interchange filing row becomes a typed FilingRecord (full row kept in `raw`).
Events are derived at the *docket* grain, not per filing: we emit a small set of
dated regulatory milestones (docket opened, application filed, order issued,
agreement approved) so `project_events` stays low-volume and high-confidence.
Routine filings (comments, notices of participation) produce a FilingRecord but
no event. v1 is metadata-only — we never open the linked PDFs.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from ..models import FilingRecord, ProjectEvent
from . import codes
from .interchange import DocketFilings

# Control number of the docket itself; used as the event/link key (see db.py).
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_date(value: Any) -> date | None:
    """Parse Interchange's M/D/YYYY filed-date string."""
    s = _clean(value)
    if s is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_row(row: dict[str, Any], *, snapshot_date: date | None = None) -> FilingRecord:
    item_type = _clean(row.get("item_type"))
    return FilingRecord(
        control_number=(_clean(row.get("control_number")) or ""),
        item_number=(_clean(row.get("item_number")) or ""),
        utility_type=_clean(row.get("utility_type")) or codes.DEFAULT_UTILITY_TYPE,
        filed_date=_parse_date(row.get("filed_date")),
        filing_party=_clean(row.get("filing_party")),
        item_type=item_type,
        item_type_label=codes.item_type_label(item_type),
        filing_description=_clean(row.get("filing_description")),
        docket_title=_clean(row.get("docket_title")),
        source_url=_clean(row.get("source_url")),
        snapshot_date=snapshot_date,
        raw={k: (_clean(v) if isinstance(v, str) else v) for k, v in row.items()},
    )


def parse_docket(
    docket: DocketFilings, *, snapshot_date: date | None = None
) -> list[FilingRecord]:
    """Normalize one docket's scraped rows into FilingRecords (usable ones only)."""
    records: list[FilingRecord] = []
    for row in docket.rows:
        record = parse_row(row, snapshot_date=snapshot_date)
        if not record.control_number or not record.item_number:
            continue
        records.append(record)
    return records


def parse_dockets(
    dockets: list[DocketFilings], *, snapshot_date: date | None = None
) -> list[FilingRecord]:
    records: list[FilingRecord] = []
    for docket in dockets:
        records.extend(parse_docket(docket, snapshot_date=snapshot_date))
    return records


def _docket_events(control_number: str, records: list[FilingRecord]) -> list[ProjectEvent]:
    """Derive milestone events for the filings of a single docket."""
    dated = [r for r in records if r.filed_date is not None]
    if not dated:
        return []
    title = next((r.docket_title for r in records if r.docket_title), None)
    events: list[ProjectEvent] = []

    # docket_opened: the earliest filing in the docket is when it became real.
    first = min(dated, key=lambda r: r.filed_date)  # type: ignore[arg-type,return-value]
    events.append(
        ProjectEvent(
            source="puct",
            permit_no=control_number,
            event_type="docket_opened",
            event_date=first.filed_date,  # type: ignore[arg-type]
            entity=title or first.filing_party,
            county=None,  # PUCT filings carry no county
            state="TX",
            raw={"item_number": first.item_number, "docket_title": title},
        )
    )

    # Milestone filings: only the item types/descriptions that signal progress.
    for rec in dated:
        etype = codes.milestone_event_type(rec.item_type, rec.filing_description)
        if etype is None:
            continue
        events.append(
            ProjectEvent(
                source="puct",
                permit_no=control_number,
                event_type=etype,
                event_date=rec.filed_date,  # type: ignore[arg-type]
                entity=rec.filing_party or title,
                county=None,
                state="TX",
                raw={
                    "item_number": rec.item_number,
                    "item_type": rec.item_type,
                    "filing_description": rec.filing_description,
                    "docket_title": title,
                },
            )
        )
    return events


def derive_all_events(records: list[FilingRecord]) -> list[ProjectEvent]:
    """Derive milestone events across dockets, de-duped on the event PK.

    The event PK is (source, permit_no, event_type, event_date); permit_no holds
    the control number. Two milestone filings of the same type on the same day in
    a docket collapse to one row.
    """
    by_docket: dict[str, list[FilingRecord]] = defaultdict(list)
    for rec in records:
        by_docket[rec.control_number].append(rec)

    seen: dict[tuple[str, str, str, str], ProjectEvent] = {}
    for control_number, docket_records in by_docket.items():
        for event in _docket_events(control_number, docket_records):
            key = (
                event.source,
                event.permit_no or "",
                event.event_type,
                event.event_date.isoformat(),
            )
            seen.setdefault(key, event)
    return list(seen.values())
