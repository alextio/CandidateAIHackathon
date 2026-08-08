"""Build an ERCOT-project-centric GeoJSON pin layer for the Texas map.

One pin == one ERCOT interconnection project (keyed by `inr`). Everything else
in the pipeline hangs off that project:

  * TCEQ air permits linked to it (via `resolved_links`) give it exact FRS
    coordinates and environmental context.
  * PUCT docket filings linked to it give it regulatory milestones.
  * `project_events` (TCEQ + PUCT) plus the ERCOT row's own dated fields are
    merged into one chronological timeline the UI shows on click.

Each feature therefore carries three things: the ERCOT core (for the pin itself),
a compact JSON `timeline` (dated milestones from every source), and a JSON
`permits` block (the TCEQ sites behind it). Derived insight fields — funnel
`stage`, milestone progress, days in queue, link confidence — are precomputed so
the map can style and rank pins without re-deriving anything client-side.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any

from supabase import Client

from .geo.county_centroids import centroid
from .tceq.db import ERCOT_TABLE, EVENTS_TABLE, LINKS_TABLE, PERMITS_TABLE

# One-row-per-project view over ERCOT_TABLE's snapshot history (migration 0005).
ERCOT_LATEST_VIEW = "ercot_projects_latest"

# ML stage classifier output (migration 0004). Keyed by the synthetic entity id
# `ercot:<inr>` this pipeline writes for every ERCOT-only project.
PREDICTIONS_TABLE = "stage_predictions"

# Human labels for the classifier's 8-class lifecycle (app.classify.stages.Stage),
# ordered earliest -> latest. Distinct from the funnel STAGE_* above: that is a
# rule-based maturity read-out for pin colour, this is the model's prediction.
PREDICTED_STAGE_LABELS = {
    "concept": "Concept",
    "fel1": "FEL1 — early planning",
    "fel2_prefeed": "FEL2 — pre-FEED",
    "feed": "FEED",
    "interconnection_agreement": "Interconnection agreement",
    "fid": "Final investment decision",
    "construction": "Construction",
    "cod": "Commercial operation",
}

# Funnel stages, in order of increasing maturity. Every project sits at the
# furthest stage it has reached — the pin colour is a maturity read-out.
STAGE_QUEUED = "queued"          # ERCOT queue only
STAGE_PERMITTING = "permitting"  # has a linked TCEQ air permit
STAGE_REGULATORY = "regulatory"  # has linked PUCT docket activity
STAGE_APPROVED = "approved"      # ERCOT approved it for energization/sync
STAGE_ORDER = [STAGE_QUEUED, STAGE_PERMITTING, STAGE_REGULATORY, STAGE_APPROVED]

# Human labels for every dated signal we can put on the timeline.
EVENT_LABELS = {
    # TCEQ (Source #2)
    "registered": "TCEQ entity registered",
    "status_change": "TCEQ status change",
    "affiliation_ended": "TCEQ affiliation ended",
    # PUCT (Source #3)
    "docket_opened": "PUCT docket opened",
    "application_filed": "Application filed at PUCT",
    "order_issued": "PUCT order issued",
    "agreement_approved": "Agreement approved",
    "filing": "PUCT filing",
    # ERCOT (Source #1, synthesized from the project row)
    "queue_seen": "Seen in ERCOT queue",
    "ia_signed": "Interconnection agreement signed",
    "approved_for_synchronization": "Approved for synchronization",
    "approved_for_energization": "Approved for energization",
    "projected_cod": "Projected commercial operation",
    "inactive": "Marked inactive",
    "cancelled": "Cancelled",
}
SOURCE_ERCOT, SOURCE_TCEQ, SOURCE_PUCT = "ercot", "tceq", "puct"


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #
# PostgREST caps a single response at ~1000 rows, so every table read here pages
# through `.range()` until a short page arrives. The two huge tables (permits,
# events) are never scanned whole — they're queried filtered to the handful of
# RNs that actually link to an ERCOT project.
_PAGE = 1000
_IN_CHUNK = 150  # keep `.in_(...)` lists short enough for the URL


def _paginate(make_query, page: int = _PAGE) -> list[dict]:
    """Run `make_query()` repeatedly over successive ranges until exhausted."""
    rows: list[dict] = []
    start = 0
    while True:
        batch = make_query().range(start, start + page - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page:
            return rows
        start += page


def _chunks(seq: list[str], n: int = _IN_CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _load_ercot(client: Client) -> dict[str, dict]:
    """inr -> current ercot_projects row, plus the earliest snapshot we've seen.

    Reads from the ``ercot_projects_latest`` view (migration 0005), which
    collapses the snapshot history to one row per project server-side — current
    state plus ``first_report`` (earliest ``report_date``, i.e. dwell time). This
    keeps the map load at ~one row per project instead of scanning every
    project × snapshot and deduping in Python.
    """
    rows = _paginate(
        lambda: client.table(ERCOT_LATEST_VIEW)
        .select(
            "inr,project_name,interconnecting_entity,poi_location,county,"
            "cdr_reporting_zone,size_category,status,capacity_mw,"
            "fuel,fuel_label,technology,technology_label,gim_study_phase,"
            "projected_cod,ia_signed,approved_for_energization,"
            "approved_for_synchronization,inactive_date,cancel_date,report_date,"
            "first_report"
        )
        .order("inr")  # stable ordering so pagination can't drop/repeat rows
    )
    out: dict[str, dict] = {}
    for row in rows:
        inr = row.get("inr")
        if not inr:
            continue
        row["_first_report"] = row.get("first_report")
        out[inr] = row
    return out


def _load_prediction(client: Client, inr: str) -> dict | None:
    """Newest ML stage prediction for one project, or None if never scored.

    Scoped to this `inr` via the synthetic `ercot:<inr>` entity id and ordered
    newest-first so the current call is the one returned.
    """
    rows = (
        client.table(PREDICTIONS_TABLE)
        .select(
            "stage,confidence,margin,expected_rank,probabilities,rule_stage,"
            "conformal_lo,conformal_hi,conformal_alpha,withdrawn,as_of,model_version"
        )
        .eq("entity_id", f"ercot:{inr}")
        .order("as_of", desc=True)
        .order("confidence", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return None
    pred = rows[0]

    probs = pred.get("probabilities") or {}
    # Rank every class high-to-low so the UI can show a probability breakdown.
    ranked = sorted(
        (
            {
                "stage": stage,
                "label": PREDICTED_STAGE_LABELS.get(stage, stage),
                "probability": prob,
            }
            for stage, prob in probs.items()
        ),
        key=lambda item: item["probability"] or 0,
        reverse=True,
    )
    rule_stage = pred.get("rule_stage")
    return {
        "stage": pred.get("stage"),
        "stage_label": PREDICTED_STAGE_LABELS.get(pred.get("stage"), pred.get("stage")),
        "confidence": pred.get("confidence"),
        "margin": pred.get("margin"),
        "expected_rank": pred.get("expected_rank"),
        "probabilities": ranked,
        "rule_stage": rule_stage,
        "agrees_with_rule": rule_stage is None or rule_stage == pred.get("stage"),
        "conformal_range": [pred.get("conformal_lo"), pred.get("conformal_hi")],
        "conformal_alpha": pred.get("conformal_alpha"),
        "withdrawn": pred.get("withdrawn"),
        "as_of": pred.get("as_of"),
        "model_version": pred.get("model_version"),
    }


def _load_links(client: Client) -> list[dict]:
    return _paginate(
        lambda: client.table(LINKS_TABLE)
        .select("source,rn_number,permit_no,inr,status,score,method,tceq_name,ercot_name")
        .order("rn_number")
    )


def _load_permits(client: Client, rns: set[str]) -> dict[str, list[dict]]:
    """rn_number -> its latest-snapshot permit rows, for the linked RNs only."""
    if not rns:
        return {}
    snap = (
        client.table(PERMITS_TABLE)
        .select("snapshot_date")
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    if not snap.data:
        return {}
    latest = snap.data[0]["snapshot_date"]
    by_rn: dict[str, list[dict]] = {}
    for chunk in _chunks(sorted(rns)):
        rows = _paginate(
            lambda ch=chunk: client.table(PERMITS_TABLE)
            .select(
                "rn_number,permit_no,entity_name,company_name,county,naics_code,"
                "naics_label,on_thesis,entity_status,latitude,longitude,"
                "geo_accuracy_m,affiliation_begin_date,status_date"
            )
            .eq("snapshot_date", latest)
            .in_("rn_number", ch)
            .order("rn_number")
            .order("permit_no")
        )
        for row in rows:
            by_rn.setdefault(row["rn_number"], []).append(row)
    return by_rn


def _load_events(
    client: Client, rns: set[str], controls: set[str]
) -> tuple[dict[str, list], dict[str, list]]:
    """Events for the linked RNs/dockets only, indexed for project attachment.

    Returns (tceq_by_rn, puct_by_control): TCEQ events keyed by rn_number, PUCT
    events keyed by control number (which lives in the event's `permit_no`).
    """
    tceq_by_rn: dict[str, list] = {}
    for chunk in _chunks(sorted(rns)):
        rows = _paginate(
            lambda ch=chunk: client.table(EVENTS_TABLE)
            .select("source,rn_number,permit_no,event_type,event_date,entity,county")
            .neq("source", SOURCE_PUCT)
            .in_("rn_number", ch)
            .order("rn_number")
            .order("event_date")
        )
        for ev in rows:
            tceq_by_rn.setdefault(ev.get("rn_number") or "", []).append(ev)

    puct_by_ctrl: dict[str, list] = {}
    for chunk in _chunks(sorted(controls)):
        rows = _paginate(
            lambda ch=chunk: client.table(EVENTS_TABLE)
            .select("source,rn_number,permit_no,event_type,event_date,entity,county")
            .eq("source", SOURCE_PUCT)
            .in_("permit_no", ch)
            .order("permit_no")
            .order("event_date")
        )
        for ev in rows:
            puct_by_ctrl.setdefault(ev.get("permit_no") or "", []).append(ev)
    return tceq_by_rn, puct_by_ctrl


# --------------------------------------------------------------------------- #
# Per-project assembly helpers
# --------------------------------------------------------------------------- #
def _looks_like_date(value: Any) -> bool:
    s = str(value)
    return len(s) >= 8 and s[:4].isdigit() and "-" in s[:7]


def _ercot_timeline(proj: dict) -> list[dict]:
    """Dated milestones synthesized from the ERCOT project row itself."""
    out: list[dict] = []

    def add(event_type: str, when: Any, *, future: bool = False) -> None:
        if not when:
            return
        out.append(
            {
                "date": str(when)[:10],
                "source": SOURCE_ERCOT,
                "type": event_type,
                "label": EVENT_LABELS.get(event_type, event_type),
                "future": future,
            }
        )

    add("queue_seen", proj.get("_first_report"))
    # ia_signed is free text in the feed ("Yes", a date, …); only date it if it
    # parses as one, otherwise it's surfaced as a boolean insight instead.
    ia = proj.get("ia_signed")
    if ia and _looks_like_date(ia):
        add("ia_signed", ia)
    add("approved_for_synchronization", proj.get("approved_for_synchronization"))
    add("approved_for_energization", proj.get("approved_for_energization"))
    add("inactive", proj.get("inactive_date"))
    add("cancelled", proj.get("cancel_date"))
    add("projected_cod", proj.get("projected_cod"), future=True)
    return out


def _mk_event(ev: dict) -> dict:
    return {
        "date": str(ev.get("event_date"))[:10],
        "source": ev.get("source"),
        "type": ev.get("event_type"),
        "label": EVENT_LABELS.get(ev.get("event_type"), ev.get("event_type")),
        "entity": ev.get("entity"),
    }


def _coords_for(proj: dict, permits: list[dict]) -> tuple[float, float, str] | None:
    """Best coordinates: exact FRS point from a linked permit, else centroid."""
    for p in permits:
        lat, lng = p.get("latitude"), p.get("longitude")
        if lat is not None and lng is not None:
            return float(lat), float(lng), "exact"
    c = centroid(proj.get("county"))
    if c is not None:
        return c[0], c[1], "county"
    return None


def _days_in_queue(proj: dict) -> int | None:
    first = proj.get("_first_report")
    if not first:
        return None
    try:
        return (date.today() - date.fromisoformat(str(first)[:10])).days
    except ValueError:
        return None


def _stage_for(has_air_permit: bool, has_regulatory: bool, approved: bool) -> str:
    """Furthest funnel stage a project has reached."""
    if approved:
        return STAGE_APPROVED
    if has_regulatory:
        return STAGE_REGULATORY
    if has_air_permit:
        return STAGE_PERMITTING
    return STAGE_QUEUED


def _links_by_inr(links: list[dict]) -> dict[str, dict]:
    """inr -> {rns, controls, best link}, from resolved_links."""
    per_inr: dict[str, dict] = {}
    for ln in links:
        inr = ln.get("inr")
        if not inr:
            continue
        agg = per_inr.setdefault(inr, {"rns": set(), "controls": set(), "best": None})
        if ln.get("source") == SOURCE_PUCT:
            if ln.get("permit_no"):
                agg["controls"].add(ln["permit_no"])
        elif ln.get("rn_number"):
            agg["rns"].add(ln["rn_number"])
        best = agg["best"]
        if best is None or (ln.get("score") or 0) > (best.get("score") or 0):
            agg["best"] = ln
    return per_inr


# --------------------------------------------------------------------------- #
# Cached base load
# --------------------------------------------------------------------------- #
# The three DB loads below are filter-independent — every /map request needs the
# same ercot rows, links, and linked-permit rows; filters are applied in Python
# afterward. Cache the assembled base for a short TTL so back-to-back loads (and
# different filter combos) don't re-scan Supabase each time. The data only
# changes when a discovery run writes new snapshots, so a small TTL is safe.
_BASE_TTL_SECONDS = 120
_base_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _load_map_base(
    client: Client,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, list[dict]]]:
    now = time.monotonic()
    cached = _base_cache["value"]
    if cached is not None and (now - _base_cache["at"]) < _BASE_TTL_SECONDS:
        return cached

    ercot = _load_ercot(client)
    per_inr = _links_by_inr(_load_links(client))

    # Coordinates need permit rows, but only for RNs that link to a project.
    needed_rns: set[str] = set()
    for inr, agg in per_inr.items():
        if inr in ercot:
            needed_rns |= agg["rns"]
    permits_by_rn = _load_permits(client, needed_rns)

    value = (ercot, per_inr, permits_by_rn)
    _base_cache.update(at=now, value=value)
    return value


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def build_map(
    client: Client,
    *,
    stage: str | None = None,
    status: str | None = None,      # active | inactive | cancelled
    county: str | None = None,
    min_mw: float | None = None,
    has_permit: bool | None = None,
    on_thesis: bool | None = None,  # has a power-generation (on-thesis) permit
    limit: int = 5000,
) -> dict[str, Any]:
    """One **lightweight** GeoJSON feature per ERCOT project.

    This is the pin layer only: identity, position, funnel stage, and small
    scalar insights for styling and ranking. The heavy per-project detail
    (timeline, permit list, milestones) is fetched on click via
    :func:`build_project_detail` — never shipped with every pin.

    We still load permits, but *only* the ones behind a resolved link, and only
    to place the pin at exact FRS coordinates. The 300k+ events table is never
    touched here.
    """
    ercot, per_inr, permits_by_rn = _load_map_base(client)

    features: list[dict[str, Any]] = []
    for inr, proj in ercot.items():
        agg = per_inr.get(inr, {"rns": set(), "controls": set(), "best": None})

        permits: list[dict] = []
        for rn in agg["rns"]:
            permits.extend(permits_by_rn.get(rn, []))
        has_air_permit = bool(agg["rns"])
        has_thesis_permit = any(p.get("on_thesis") for p in permits)
        has_regulatory = bool(agg["controls"])
        approved = bool(
            proj.get("approved_for_energization")
            or proj.get("approved_for_synchronization")
        )
        st = _stage_for(has_air_permit, has_regulatory, approved)

        # --- filters ---------------------------------------------------------
        if stage and st != stage:
            continue
        if status and (proj.get("status") or "active") != status:
            continue
        if county and (proj.get("county") or "").upper() != county.upper():
            continue
        if min_mw is not None:
            cap = proj.get("capacity_mw")
            if cap is None or cap < min_mw:
                continue
        if has_permit is not None and has_air_permit != has_permit:
            continue
        if on_thesis is not None and has_thesis_permit != on_thesis:
            continue

        loc = _coords_for(proj, permits)
        if loc is None:
            continue
        lat, lng, precision = loc

        milestones_hit = 1 + sum(
            [has_air_permit, has_regulatory, bool(proj.get("ia_signed")), approved]
        )
        best = agg["best"] or {}
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
                "properties": {
                    "inr": inr,
                    "name": proj.get("interconnecting_entity") or proj.get("project_name"),
                    "project_name": proj.get("project_name"),
                    "county": proj.get("county"),
                    "state": "TX",
                    "status": proj.get("status") or "active",
                    "capacity_mw": proj.get("capacity_mw"),
                    "fuel": proj.get("fuel_label") or proj.get("fuel"),
                    "technology": proj.get("technology_label") or proj.get("technology"),
                    "stage": st,
                    "precision": precision,
                    "has_air_permit": has_air_permit,
                    "has_thesis_permit": has_thesis_permit,
                    "has_regulatory": has_regulatory,
                    "permit_count": len(agg["rns"]),
                    "milestones_hit": milestones_hit,
                    "milestones_total": 5,
                    "days_in_queue": _days_in_queue(proj),
                    "match_confidence": best.get("score"),
                },
            }
        )

    if len(features) > limit:  # keep the largest projects when capped
        features.sort(key=lambda f: f["properties"].get("capacity_mw") or 0, reverse=True)
        features = features[:limit]

    by_stage: dict[str, int] = {s: 0 for s in STAGE_ORDER}
    for f in features:
        by_stage[f["properties"]["stage"]] = by_stage.get(f["properties"]["stage"], 0) + 1
    counts = {
        "total": len(features),
        "exact": sum(1 for f in features if f["properties"]["precision"] == "exact"),
        "county": sum(1 for f in features if f["properties"]["precision"] == "county"),
        "with_permit": sum(1 for f in features if f["properties"]["has_air_permit"]),
        "with_regulatory": sum(1 for f in features if f["properties"]["has_regulatory"]),
        "capacity_mw": round(sum(f["properties"].get("capacity_mw") or 0 for f in features)),
    }
    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {"counts": counts, "by_stage": by_stage},
    }


def build_project_detail(client: Client, inr: str) -> dict[str, Any] | None:
    """Full dossier for one ERCOT project — loaded on click, not up front.

    Every query here is scoped to this `inr` (and its handful of linked RNs /
    dockets), so it stays cheap no matter how large the permit/event tables are.
    Returns ``None`` if the project id is unknown.
    """
    rows = _paginate(
        lambda: client.table(ERCOT_TABLE)
        .select(
            "inr,project_name,interconnecting_entity,poi_location,county,"
            "cdr_reporting_zone,size_category,status,capacity_mw,"
            "fuel,fuel_label,technology,technology_label,gim_study_phase,"
            "projected_cod,ia_signed,approved_for_energization,"
            "approved_for_synchronization,inactive_date,cancel_date,report_date"
        )
        .eq("inr", inr)
        .order("report_date", desc=True)
    )
    if not rows:
        return None
    proj = rows[0]  # newest snapshot
    proj["_first_report"] = min(
        (r.get("report_date") for r in rows if r.get("report_date")), default=None
    )

    links = _paginate(
        lambda: client.table(LINKS_TABLE)
        .select("source,rn_number,permit_no,inr,status,score,method,tceq_name,ercot_name")
        .eq("inr", inr)
    )
    agg = _links_by_inr(links).get(inr, {"rns": set(), "controls": set(), "best": None})

    permits_by_rn = _load_permits(client, agg["rns"])
    permits: list[dict] = []
    for rn in agg["rns"]:
        permits.extend(permits_by_rn.get(rn, []))
    tceq_events, puct_events = _load_events(client, agg["rns"], agg["controls"])

    # --- merged, deduped, date-sorted timeline ------------------------------
    # A single site holds many permits, each emitting the same dated signal
    # (e.g. "registered"), so collapse identical (date, source, type, entity).
    raw = _ercot_timeline(proj)
    for rn in agg["rns"]:
        raw += [_mk_event(e) for e in tceq_events.get(rn, [])]
    for ctrl in agg["controls"]:
        raw += [_mk_event(e) for e in puct_events.get(ctrl, [])]
    seen: set[tuple] = set()
    timeline: list[dict] = []
    for t in raw:
        key = (t.get("date"), t.get("source"), t.get("type"), t.get("entity"))
        if key in seen:
            continue
        seen.add(key)
        timeline.append(t)
    timeline.sort(key=lambda t: (t.get("date") or "", t.get("future", False)))
    past = [t for t in timeline if not t.get("future")]

    has_air_permit = bool(agg["rns"])
    has_regulatory = bool(agg["controls"])
    approved = bool(
        proj.get("approved_for_energization") or proj.get("approved_for_synchronization")
    )
    milestones = {
        "in_queue": True,
        "air_permit": has_air_permit,
        "regulatory": has_regulatory,
        "ia_signed": bool(proj.get("ia_signed")),
        "approved": approved,
    }
    best = agg["best"] or {}
    prediction = _load_prediction(client, inr)

    return {
        "inr": inr,
        "name": proj.get("interconnecting_entity") or proj.get("project_name"),
        "project_name": proj.get("project_name"),
        "county": proj.get("county"),
        "state": "TX",
        "zone": proj.get("cdr_reporting_zone"),
        "poi_location": proj.get("poi_location"),
        "size_category": proj.get("size_category"),
        "status": proj.get("status") or "active",
        "capacity_mw": proj.get("capacity_mw"),
        "fuel": proj.get("fuel_label") or proj.get("fuel"),
        "technology": proj.get("technology_label") or proj.get("technology"),
        "gim_study_phase": proj.get("gim_study_phase"),
        "projected_cod": proj.get("projected_cod"),
        "stage": _stage_for(has_air_permit, has_regulatory, approved),
        "stage_prediction": prediction,
        "days_in_queue": _days_in_queue(proj),
        "event_count": len(past),
        "latest_event": past[-1]["label"] if past else None,
        "latest_event_date": past[-1]["date"] if past else None,
        "match_confidence": best.get("score"),
        "match_status": best.get("status"),
        "milestones": milestones,
        "milestones_hit": sum(1 for v in milestones.values() if v),
        "milestones_total": len(milestones),
        "timeline": timeline,
        "permits": [
            {
                "rn_number": p.get("rn_number"),
                "permit_no": p.get("permit_no"),
                "site_name": p.get("entity_name"),
                "company": p.get("company_name"),
                "county": p.get("county"),
                "naics_label": p.get("naics_label"),
                "on_thesis": p.get("on_thesis"),
                "entity_status": p.get("entity_status"),
                "precision": "exact" if p.get("latitude") is not None else "county",
                "geo_accuracy_m": p.get("geo_accuracy_m"),
            }
            for p in permits
        ],
    }
