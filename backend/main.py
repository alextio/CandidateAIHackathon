"""Texas project-discovery API.

Source #1 — ERCOT: Public MIS GIS Report (interconnection queue) -> projects.
Source #2 — TCEQ: Central Registry air NSR permits -> permit records + events.
Derived  — stage classifier: both sources -> a lifecycle position per project.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.classify import NoDataError, Stage, run_classification, run_labels, run_momentum
from app.classify import store as classify_store
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


# ---------------------------------------------------------------------------
# Stage classifier
#
# The classifier reads what the two discovery sources already persisted, so it
# has no fetch step of its own — POST /classify is pure computation over
# ercot_projects + tceq_permits + resolved_links.
# ---------------------------------------------------------------------------
def _classify_client():
    settings = get_settings()
    if not settings.supabase_configured:
        raise HTTPException(
            status_code=400,
            detail="Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY.",
        )
    return get_client(settings)


def _as_of(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"as_of must be YYYY-MM-DD, got {value!r}"
        ) from exc


@app.post("/classify")
def classify(
    as_of: str | None = Query(
        None,
        description="Reconstruct what was knowable at this date, YYYY-MM-DD. "
        "Defaults to the newest report month.",
    ),
    persist: bool = Query(True, description="Write model_runs + stage_predictions"),
    include_momentum: bool = Query(True, description="Also write project_momentum"),
    alpha: float = Query(
        0.10,
        gt=0.0,
        lt=1.0,
        description="Conformal miscoverage rate: intervals cover the true stage "
        "at least (1 - alpha) of the time",
    ),
    seed: int = Query(20260808, description="Split and solver seed"),
    notes: str | None = Query(None, description="Free text stored on the model run"),
):
    """Label with the rules, fit, calibrate, score every project, and persist.

    Training and scoring happen in one pass, so the model never has to be
    written to disk. `model_version` is a content hash of the training inputs:
    re-running on unchanged data reproduces the same version rather than
    stacking near-identical model rows.

    `persist=false` runs everything and returns the summary without writing —
    the fast way to check the weak labels before committing to a model run.
    """
    client = _classify_client()
    try:
        return run_classification(
            client,
            as_of=_as_of(as_of),
            alpha=alpha,
            seed=seed,
            notes=notes,
            persist=persist,
            include_momentum=include_momentum,
        )
    except NoDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # not enough usable labels to train on
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/classify/labels")
def classify_labels(
    as_of: str | None = Query(None, description="YYYY-MM-DD; defaults to the newest report"),
):
    """Weak-label coverage without training. Read-only.

    Worth reading before every run: `missing_stages` lists the classes no rule
    can label, and those can never be predicted no matter how the model scores.
    """
    client = _classify_client()
    try:
        return run_labels(client, as_of=_as_of(as_of))
    except NoDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/classify/momentum")
def classify_momentum(
    as_of: str | None = Query(None, description="YYYY-MM-DD; defaults to the newest report"),
    persist: bool = Query(True, description="Upsert into project_momentum"),
):
    """Recompute the snapshot-series metrics on their own, without training."""
    client = _classify_client()
    try:
        return run_momentum(client, as_of=_as_of(as_of), persist=persist)
    except NoDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/stages")
def list_stages(
    stage: str | None = Query(
        None, description="Lifecycle stage: " + " | ".join(s.value for s in Stage)
    ),
    as_of: str | None = Query(None, description="Snapshot date, YYYY-MM-DD"),
    model_version: str | None = Query(None, description="Defaults to whatever is stored"),
    min_confidence: float | None = Query(
        None, ge=0.0, le=1.0, description="Calibrated probability of the top class"
    ),
    limit: int = Query(100, le=1000),
    offset: int = 0,
):
    """Stored stage predictions, newest and most confident first.

    `confidence` is calibrated, so 0.7 means roughly seven in ten right. Pair it
    with `margin` before trusting a call: 0.5 confidence with 0.45 margin is
    decisive, 0.5 with 0.02 is a coin flip between two adjacent stages.
    """
    if stage and stage not in {s.value for s in Stage}:
        raise HTTPException(
            status_code=400,
            detail=f"stage must be one of: {', '.join(s.value for s in Stage)}",
        )

    client = _classify_client()
    try:
        results = classify_store.fetch_predictions(
            client,
            stage=stage,
            as_of=_as_of(as_of),
            model_version=model_version,
            min_confidence=min_confidence,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"limit": limit, "offset": offset, "results": results}


@app.get("/momentum")
def list_momentum(
    grade: str | None = Query(
        None, description="accelerating | on_track | slipping | stalled | unknown"
    ),
    as_of: str | None = Query(None, description="Snapshot date, YYYY-MM-DD"),
    limit: int = Query(100, le=1000),
    offset: int = 0,
):
    """Stored momentum metrics.

    `cod_slip_rate` is the number to read: ~0 holding schedule, ~1 slipping a
    month per month, negative pulling in. Null below three snapshots, because
    "cannot tell yet" is not "not moving".
    """
    client = _classify_client()
    try:
        results = classify_store.fetch_momentum(
            client, grade=grade, as_of=_as_of(as_of), limit=limit, offset=offset
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"limit": limit, "offset": offset, "results": results}


@app.get("/projects/{inr}/stage")
def project_stage(inr: str):
    """Everything the classifier knows about one project.

    Returns the newest prediction, its momentum row, and the rule the weak label
    came from — so a disagreement between rule and model is visible here rather
    than only in aggregate.
    """
    client = _classify_client()
    try:
        predictions = classify_store.fetch_predictions(client, inr=inr, limit=1)
        momentum = classify_store.fetch_momentum(client, inr=inr, limit=1)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not predictions:
        raise HTTPException(
            status_code=404,
            detail=f"no stage prediction for {inr} — run POST /classify first",
        )

    prediction = predictions[0]
    return {
        "inr": inr,
        "entity_id": prediction["entity_id"],
        "as_of": prediction["as_of"],
        "stage": prediction["stage"],
        "label_source": prediction["label_source"],
        "rule_stage": prediction["rule_stage"],
        "agrees_with_rule": prediction["rule_stage"] is None
        or prediction["rule_stage"] == prediction["stage"],
        "confidence": prediction["confidence"],
        "margin": prediction["margin"],
        "entropy": prediction["entropy"],
        "expected_rank": prediction["expected_rank"],
        "conformal_range": [prediction["conformal_lo"], prediction["conformal_hi"]],
        "conformal_alpha": prediction["conformal_alpha"],
        "withdrawn": prediction["withdrawn"],
        "probabilities": prediction["probabilities"],
        "contributions": prediction["contributions"],
        "justification": prediction["justification"],
        "model_version": prediction["model_version"],
        "momentum": momentum[0] if momentum else None,
    }
