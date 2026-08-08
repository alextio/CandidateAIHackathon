"""Texas project-discovery API.

Source #1 — ERCOT: Public MIS GIS Report (interconnection queue) -> projects.
Source #2 — TCEQ: Central Registry air NSR permits -> permit records + events.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import TABLE, get_client
from app.discovery import discover
from app.ercot.codes import DATA_CENTER_TECHNOLOGIES
from app.map_view import build_map
from app.tceq import discovery as tceq_discovery
from app.tceq.central_registry import REGION_DATASETS
from app.tceq.db import EVENTS_TABLE

app = FastAPI(title="Texas Project Discovery", version="0.2.0")

# Allow the frontend (any origin) to call the API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    settings = get_settings()
    return {
        "service": "texas-project-discovery",
        "sources": {
            "ercot": "ERCOT GIS Report (PG7-200-ER, report type 15933)",
            "tceq": "TCEQ Central Registry air NSR permits (data.texas.gov / Socrata)",
        },
        "supabase_configured": settings.supabase_configured,
    }


@app.post("/discover/ercot")
def discover_ercot(
    months: int = Query(12, ge=1, le=23, description="How many recent monthly reports to pull"),
    persist: bool = Query(True, description="Upsert results into Supabase"),
    include_projects: bool = Query(
        False, description="Return the full project list in the response"
    ),
    all_technologies: bool = Query(
        False,
        description="Keep every technology instead of only the data-center set "
        "(CC, GT, IC, BA, EN, FC)",
    ),
):
    """Fetch the last `months` GIS Reports, parse all sheets, and (optionally) persist."""
    settings = get_settings()
    try:
        result, projects = discover(
            settings,
            persist=persist,
            months=months,
            technologies=None if all_technologies else DATA_CENTER_TECHNOLOGIES,
        )
    except Exception as exc:  # surface fetch/parse/db errors clearly
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    body = asdict(result)
    if not settings.supabase_configured and persist:
        body["note"] = "Supabase not configured; parsed only. Set SUPABASE_* to persist."
    if include_projects:
        body["projects"] = [p.model_dump(mode="json") for p in projects]
    return body


@app.post("/discover/tceq")
def discover_tceq(
    regions: list[str] | None = Query(
        None,
        description="Regional datasets to pull; omit for all of Texas. One or more of: "
        + ", ".join(REGION_DATASETS),
    ),
    persist: bool = Query(True, description="Upsert results into Supabase"),
    resolve: bool = Query(
        True, description="Fuzzy-link permits to ercot_projects (requires Supabase)"
    ),
    on_thesis_only: bool = Query(
        False,
        description="Keep only electric-power-generation NAICS rows instead of all AIRNSR",
    ),
    geocode: bool = Query(
        True, description="Look up exact lat/long per RN from EPA FRS (Tier-2 pins)"
    ),
    max_rows_per_region: int | None = Query(
        None, ge=1, description="Cap rows pulled per region (for quick tests)"
    ),
    include_records: bool = Query(
        False, description="Return the full permit + event lists in the response"
    ),
):
    """Fetch TCEQ AIRNSR permits, derive events, resolve to ERCOT, and (optionally) persist."""
    settings = get_settings()
    if regions:
        unknown = [r for r in regions if r not in REGION_DATASETS]
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"Unknown region(s): {', '.join(unknown)}"
            )
    try:
        result, records, events, links = tceq_discovery.discover(
            settings,
            persist=persist,
            regions=regions,
            on_thesis_only=on_thesis_only,
            resolve=resolve,
            geocode_permits=geocode,
            max_rows_per_region=max_rows_per_region,
        )
    except Exception as exc:  # surface fetch/parse/db errors clearly
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    body = asdict(result)
    if not settings.supabase_configured and (persist or resolve):
        body["note"] = "Supabase not configured; parsed only. Set SUPABASE_* to persist/resolve."
    if include_records:
        body["permits"] = [r.model_dump(mode="json") for r in records]
        body["events"] = [e.model_dump(mode="json") for e in events]
        body["links"] = [
            {
                "rn_number": link.rn_number,
                "permit_no": link.permit_no,
                "inr": link.inr,
                "status": link.status,
                "score": link.score,
                "method": link.method,
                "tceq_name": link.tceq_name,
                "ercot_name": link.ercot_name,
                "county": link.county,
            }
            for link in links
        ]
    return body


@app.get("/events")
def list_events(
    entity: str | None = Query(None, description="Substring match on the entity/company name"),
    county: str | None = None,
    event_type: str | None = Query(
        None, description="registered | status_change | affiliation_ended"
    ),
    source: str | None = Query(None, description="Pipeline source, e.g. tceq"),
    since: str | None = Query(None, description="Only events on/after this date, YYYY-MM-DD"),
    until: str | None = Query(None, description="Only events on/before this date, YYYY-MM-DD"),
    limit: int = Query(100, le=1000),
    offset: int = 0,
):
    """Query derived project events with filters (requires Supabase)."""
    settings = get_settings()
    if not settings.supabase_configured:
        raise HTTPException(
            status_code=400,
            detail="Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY.",
        )
    client = get_client(settings)
    q = client.table(EVENTS_TABLE).select("*", count="exact")
    if entity:
        q = q.ilike("entity", f"%{entity}%")
    if county:
        q = q.ilike("county", county)
    if event_type:
        q = q.eq("event_type", event_type)
    if source:
        q = q.eq("source", source)
    if since:
        q = q.gte("event_date", since)
    if until:
        q = q.lte("event_date", until)
    q = q.order("event_date", desc=True).range(offset, offset + limit - 1)
    resp = q.execute()
    return {"count": resp.count, "limit": limit, "offset": offset, "results": resp.data}


@app.get("/map")
def map_projects(
    source: str = Query("all", description="all | tceq | ercot"),
    stage: str | None = Query(
        None, description="queued | permitting | permit_only"
    ),
    resolved_only: bool = Query(
        False, description="Only permits confidently linked to an ERCOT project"
    ),
    on_thesis: bool | None = Query(
        None, description="Filter TCEQ pins to electric-power-generation NAICS"
    ),
    county: str | None = Query(None, description="County name filter"),
    min_mw: float | None = Query(None, description="Minimum ERCOT capacity (MW)"),
    limit: int = Query(5000, le=20000),
):
    """GeoJSON pin layer for the Texas map.

    One de-duplicated feature per project: TCEQ permit sites (exact FRS
    coordinates where available, else county centroid) plus ERCOT queue projects
    with no permit yet (county centroids). Each feature's `stage` and `precision`
    encode how far along the project is.
    """
    settings = get_settings()
    if not settings.supabase_configured:
        raise HTTPException(
            status_code=400,
            detail="Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY.",
        )
    if source not in ("all", "tceq", "ercot"):
        raise HTTPException(status_code=400, detail="source must be all | tceq | ercot")
    client = get_client(settings)
    try:
        return build_map(
            client,
            source=source,
            stage=stage,
            resolved_only=resolved_only,
            on_thesis=on_thesis,
            county=county,
            min_mw=min_mw,
            limit=limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/projects")
def list_projects(
    status: str | None = Query(None, description="active | inactive | cancelled"),
    fuel: str | None = Query(None, description="Fuel code, e.g. SOL, WIN, GAS"),
    county: str | None = None,
    state: str = Query("TX", description="State filter (ERCOT is Texas-only)"),
    zone: str | None = Query(None, description="CDR reporting zone, e.g. WEST"),
    min_mw: float | None = Query(None, description="Minimum capacity in MW"),
    report_date: str | None = Query(
        None, description="Snapshot month, YYYY-MM-DD (e.g. 2026-07-01)"
    ),
    limit: int = Query(100, le=1000),
    offset: int = 0,
):
    """Query persisted projects with filters (requires Supabase)."""
    settings = get_settings()
    if not settings.supabase_configured:
        raise HTTPException(
            status_code=400,
            detail="Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY.",
        )
    client = get_client(settings)
    q = client.table(TABLE).select("*", count="exact")
    if status:
        q = q.eq("status", status)
    if fuel:
        q = q.eq("fuel", fuel.upper())
    if county:
        q = q.ilike("county", county)
    if state:
        q = q.eq("state", state.upper())
    if zone:
        q = q.eq("cdr_reporting_zone", zone.upper())
    if min_mw is not None:
        q = q.gte("capacity_mw", min_mw)
    if report_date:
        q = q.eq("report_date", report_date)
    q = (
        q.order("report_date", desc=True)
        .order("capacity_mw", desc=True)
        .range(offset, offset + limit - 1)
    )
    resp = q.execute()
    return {"count": resp.count, "limit": limit, "offset": offset, "results": resp.data}
