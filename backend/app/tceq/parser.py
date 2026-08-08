"""Normalize raw TCEQ Central Registry rows into PermitRecord + ProjectEvent.

The Central Registry is affiliation-level: one row ties a regulated entity (RN)
to a program (AIRNSR) via an additional id (the NSR permit number). We keep the
whole row in `raw` and lift the fields we query/resolve on into typed columns,
cleaning TCEQ's sentinel dates (1800-01-01 / 3000-12-31) to None.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..models import PermitRecord, ProjectEvent
from . import codes


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_date(value: Any) -> date | None:
    """Parse a Socrata floating-timestamp string, dropping TCEQ sentinels."""
    s = _clean(value)
    if s is None:
        return None
    day = s[:10]  # 'YYYY-MM-DDT00:00:00.000' -> 'YYYY-MM-DD'
    if day in codes.SENTINEL_DATES:
        return None
    try:
        return datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_row(
    row: dict[str, Any],
    *,
    region: str | None = None,
    source_dataset: str | None = None,
    snapshot_date: date | None = None,
) -> PermitRecord:
    naics_code, naics_label = codes.parse_naics(row.get("indus_type_cd_name"))
    return PermitRecord(
        rn_number=(_clean(row.get("ref_num_txt")) or ""),
        permit_no=_clean(row.get("additional_id_text")),
        program_code=_clean(row.get("program_code")),
        entity_name=_clean(row.get("reg_ent_name")),
        company_name=_clean(row.get("princ_name")),
        company_legal_name=_clean(row.get("princ_legal_name")),
        county=_clean(row.get("re_phys_loc_addr_county")),
        city=_clean(row.get("re_phys_loc_city")),
        address=_clean(row.get("re_phys_loc_addr_line_1")),
        naics_code=naics_code,
        naics_label=naics_label,
        on_thesis=codes.is_power_generation(naics_code),
        entity_status=codes.entity_status(row.get("reg_ent_status_txt")),
        company_status=_clean(row.get("princ_status_txt")),
        affiliation_begin_date=_parse_date(row.get("affil_begin_dt")),
        affiliation_end_date=_parse_date(row.get("affil_end_dt")),
        status_date=_parse_date(row.get("status_dt")),
        region=region or _clean(row.get("tceq_region_number")),
        source_dataset=source_dataset,
        snapshot_date=snapshot_date,
        raw={k: _clean(v) if isinstance(v, str) else v for k, v in row.items()},
    )


def parse_rows(
    rows: list[dict[str, Any]],
    *,
    region: str | None = None,
    source_dataset: str | None = None,
    snapshot_date: date | None = None,
    on_thesis_only: bool = False,
) -> list[PermitRecord]:
    """Normalize a batch of raw rows into PermitRecords.

    Rows without a usable RN number are skipped (nothing to key on). When
    `on_thesis_only` is set, only electric-power-generation NAICS rows are kept;
    otherwise everything is kept and `on_thesis` is left as a flag so the
    ERCOT-resolution join can still promote off-thesis rows.
    """
    records: list[PermitRecord] = []
    for row in rows:
        record = parse_row(
            row,
            region=region,
            source_dataset=source_dataset,
            snapshot_date=snapshot_date,
        )
        if not record.rn_number:
            continue
        if on_thesis_only and not record.on_thesis:
            continue
        records.append(record)
    return records


# The events a Central Registry row can testify to. This feed is not
# transaction-level, so we surface the dated affiliation signals it does carry:
#   registered         -> the entity was affiliated to the NSR program
#   status_change      -> the affiliation/registration status last changed
#   affiliation_ended  -> a real (non-sentinel) affiliation end date
def derive_events(record: PermitRecord) -> list[ProjectEvent]:
    """Derive dated 'getting real' events from one permit record."""
    entity = record.company_name or record.entity_name
    base = dict(
        source="tceq",
        permit_no=record.permit_no,
        rn_number=record.rn_number,
        entity=entity,
        county=record.county,
    )
    events: list[ProjectEvent] = []
    if record.affiliation_begin_date is not None:
        events.append(
            ProjectEvent(event_type="registered", event_date=record.affiliation_begin_date, **base)
        )
    if record.status_date is not None:
        events.append(
            ProjectEvent(event_type="status_change", event_date=record.status_date, **base)
        )
    if record.affiliation_end_date is not None:
        events.append(
            ProjectEvent(
                event_type="affiliation_ended", event_date=record.affiliation_end_date, **base
            )
        )
    return events


def derive_all_events(records: list[PermitRecord]) -> list[ProjectEvent]:
    """Derive events across many records, de-duplicated on the event's key.

    The event PK is (source, permit_no, event_type, event_date); collapse to it
    so a permit that recurs across regions/rows produces one row per signal.
    """
    seen: dict[tuple[str, str, str, str], ProjectEvent] = {}
    for record in records:
        for event in derive_events(record):
            key = (
                event.source,
                event.permit_no or "",
                event.event_type,
                event.event_date.isoformat(),
            )
            seen.setdefault(key, event)
    return list(seen.values())
