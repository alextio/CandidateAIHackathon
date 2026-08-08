"""Normalized project model shared by parser, DB layer, and API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ProjectStatus = Literal["active", "inactive", "cancelled"]
SizeCategory = Literal["Large", "Small"]


class Project(BaseModel):
    """One interconnection project (a single row of a GIS Report sheet).

    Typed columns cover the fields we query on; `raw` keeps every original
    cell keyed by its source header so nothing from the report is lost.
    """

    inr: str  # Interconnection Request Number — natural key
    project_name: str | None = None
    size_category: SizeCategory | None = None
    status: ProjectStatus = "active"

    interconnecting_entity: str | None = None
    poi_location: str | None = None
    county: str | None = None
    state: str = "TX"  # ERCOT's footprint is entirely within Texas
    cdr_reporting_zone: str | None = None

    fuel: str | None = None
    fuel_label: str | None = None
    technology: str | None = None
    technology_label: str | None = None
    capacity_mw: float | None = None

    gim_study_phase: str | None = None
    projected_cod: date | None = None
    ia_signed: str | None = None
    approved_for_energization: date | None = None
    approved_for_synchronization: date | None = None

    # status-sheet specifics
    inactive_date: datetime | None = None
    cancel_date: datetime | None = None

    comment: str | None = None

    # provenance
    report_date: date | None = None
    source_sheet: str | None = None
    source_file: str | None = None

    # everything from the source row, header -> value (JSON-serializable)
    raw: dict[str, Any] = Field(default_factory=dict)


# --- Source #2: TCEQ air permits (Central Registry, program_code=AIRNSR) ---

EntityStatus = Literal["active", "inactive", "unknown"]
# The event types we can derive from the TCEQ Central Registry. This source is
# affiliation-level, not application-transaction level, so we cannot cleanly
# split "application filed" from "permit issued"; these are the dated signals
# the feed actually exposes.
EventType = Literal["registered", "status_change", "affiliation_ended"]


class PermitRecord(BaseModel):
    """One TCEQ air (New Source Review) permit affiliation for a regulated entity.

    Typed columns cover what we query/resolve on; `raw` keeps every original
    Socrata field so nothing from the source row is lost.
    """

    rn_number: str  # Regulated Entity number, e.g. RN104089412 (natural key part)
    permit_no: str | None = None  # NSR permit number (additional_id_text)
    program_code: str | None = None  # e.g. AIRNSR

    entity_name: str | None = None  # reg_ent_name — the site/facility name
    company_name: str | None = None  # princ_name — the affiliated customer (CN)
    company_legal_name: str | None = None  # princ_legal_name

    county: str | None = None
    city: str | None = None
    address: str | None = None
    state: str = "TX"  # TCEQ's footprint is entirely within Texas

    naics_code: str | None = None
    naics_label: str | None = None
    on_thesis: bool = False  # NAICS falls in the electric-power-generation allowlist

    entity_status: EntityStatus = "unknown"
    company_status: str | None = None

    # exact coordinates from EPA FRS (Tier-2 geocoding); null -> use county centroid
    latitude: float | None = None
    longitude: float | None = None
    geo_accuracy_m: int | None = None
    geo_source: str | None = None  # e.g. 'epa_frs'

    # cleaned dates (source sentinels 1800-01-01 / 3000-12-31 -> None)
    affiliation_begin_date: date | None = None
    affiliation_end_date: date | None = None
    status_date: date | None = None

    # provenance
    region: str | None = None  # which regional dataset it came from
    source_dataset: str | None = None  # Socrata four-by-four id
    snapshot_date: date | None = None  # the run date this snapshot was pulled

    # every original Socrata field, name -> value (JSON-serializable)
    raw: dict[str, Any] = Field(default_factory=dict)


class ProjectEvent(BaseModel):
    """A dated 'project is getting real' signal derived from a permit record."""

    source: str = "tceq"
    permit_no: str | None = None
    rn_number: str | None = None
    event_type: EventType
    event_date: date

    entity: str | None = None  # best available name for the actor/site
    county: str | None = None
    state: str = "TX"

    raw: dict[str, Any] = Field(default_factory=dict)
