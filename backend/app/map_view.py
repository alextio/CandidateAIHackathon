"""Build a project-centric GeoJSON pin layer for the Texas map.

The map is not three source tables — it's one de-duplicated set of projects. We
start from TCEQ permit sites (which can carry exact FRS coordinates), fold in the
ERCOT project each one resolves to, and then add ERCOT queue projects that have
no permit yet (county-centroid pins). Every feature reports its `stage` (how far
along the project is) and its `precision` (exact vs. county centroid), so pin
placement itself communicates project maturity.
"""
from __future__ import annotations

from typing import Any

from supabase import Client

from .geo.county_centroids import centroid
from .tceq.db import ERCOT_TABLE, LINKS_TABLE, PERMITS_TABLE

# Funnel stages, in order of increasing commitment.
STAGE_QUEUED = "queued"        # ERCOT queue only, no permit yet
STAGE_PERMITTING = "permitting"  # has a TCEQ air permit AND ties to a queue project
STAGE_PERMIT_ONLY = "permit_only"  # TCEQ air permit with no ERCOT match


def _latest_snapshot_date(client: Client) -> str | None:
    resp = (
        client.table(PERMITS_TABLE)
        .select("snapshot_date")
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0]["snapshot_date"] if resp.data else None


def _load_links(client: Client) -> dict[tuple[str, str], dict]:
    """(rn_number, permit_no) -> link row, for the latest resolution."""
    resp = (
        client.table(LINKS_TABLE)
        .select("rn_number,permit_no,inr,status,score")
        .execute()
    )
    out: dict[tuple[str, str], dict] = {}
    for row in resp.data or []:
        out[(row["rn_number"], row.get("permit_no") or "")] = row
    return out


def _load_ercot(client: Client, limit: int) -> dict[str, dict]:
    """inr -> latest ercot_projects row (dedup newest snapshot per project)."""
    resp = (
        client.table(ERCOT_TABLE)
        .select(
            "inr,project_name,interconnecting_entity,county,capacity_mw,"
            "fuel,fuel_label,technology,technology_label,status,report_date"
        )
        .order("report_date", desc=True)
        .limit(limit)
        .execute()
    )
    out: dict[str, dict] = {}
    for row in resp.data or []:
        inr = row.get("inr")
        if inr and inr not in out:  # first == newest
            out[inr] = row
    return out


def _point(lat: float, lng: float, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": props,
    }


def _coords(lat, lng, county) -> tuple[float, float, str] | None:
    """Resolve (lat, lng, precision): exact if present, else county centroid."""
    if lat is not None and lng is not None:
        return float(lat), float(lng), "exact"
    c = centroid(county)
    if c is not None:
        return c[0], c[1], "county"
    return None


def build_map(
    client: Client,
    *,
    source: str = "all",          # all | tceq | ercot
    stage: str | None = None,
    resolved_only: bool = False,
    on_thesis: bool | None = None,
    county: str | None = None,
    min_mw: float | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    """Assemble the GeoJSON FeatureCollection of project pins."""
    links = _load_links(client)
    ercot = _load_ercot(client, limit=max(limit, 20000))

    features: list[dict[str, Any]] = []
    linked_inrs: set[str] = set()

    # --- TCEQ permit sites (exact coords where FRS had them) ---------------
    if source in ("all", "tceq"):
        snap = _latest_snapshot_date(client)
        if snap is not None:
            q = (
                client.table(PERMITS_TABLE)
                .select(
                    "rn_number,permit_no,entity_name,company_name,county,"
                    "naics_code,naics_label,on_thesis,entity_status,"
                    "latitude,longitude,geo_accuracy_m"
                )
                .eq("snapshot_date", snap)
            )
            if on_thesis is not None:
                q = q.eq("on_thesis", on_thesis)
            if county:
                q = q.ilike("county", county)
            q = q.limit(limit)
            for row in q.execute().data or []:
                link = links.get((row["rn_number"], row.get("permit_no") or ""))
                inr = link.get("inr") if link else None
                status = link.get("status") if link else None
                if resolved_only and status != "resolved":
                    continue
                proj = ercot.get(inr) if inr else None
                if proj:
                    linked_inrs.add(inr)
                    st = STAGE_PERMITTING
                else:
                    st = STAGE_PERMIT_ONLY
                if stage and st != stage:
                    continue
                if min_mw is not None:
                    cap = (proj or {}).get("capacity_mw")
                    if cap is None or cap < min_mw:
                        continue
                loc = _coords(row.get("latitude"), row.get("longitude"), row.get("county"))
                if loc is None:
                    continue
                lat, lng, precision = loc
                features.append(
                    _point(
                        lat,
                        lng,
                        {
                            "layer": "tceq",
                            "stage": st,
                            "id": f"tceq:{row['rn_number']}:{row.get('permit_no') or ''}",
                            "name": row.get("company_name") or row.get("entity_name"),
                            "site_name": row.get("entity_name"),
                            "county": row.get("county"),
                            "state": "TX",
                            "rn_number": row["rn_number"],
                            "permit_no": row.get("permit_no") or None,
                            "naics_code": row.get("naics_code"),
                            "on_thesis": row.get("on_thesis"),
                            "precision": precision,
                            "geo_accuracy_m": row.get("geo_accuracy_m"),
                            # ERCOT enrichment when linked
                            "inr": inr,
                            "project_name": (proj or {}).get("project_name"),
                            "capacity_mw": (proj or {}).get("capacity_mw"),
                            "fuel": (proj or {}).get("fuel"),
                            "technology": (proj or {}).get("technology"),
                            "resolution_status": status,
                            "resolution_score": link.get("score") if link else None,
                        },
                    )
                )

    # --- ERCOT queue projects with no permit yet (county centroids) --------
    if source in ("all", "ercot") and not resolved_only:
        if stage in (None, STAGE_QUEUED):
            for inr, proj in ercot.items():
                if inr in linked_inrs:
                    continue  # already shown as a permitting pin
                if county and (proj.get("county") or "").upper() != county.upper():
                    continue
                if min_mw is not None:
                    cap = proj.get("capacity_mw")
                    if cap is None or cap < min_mw:
                        continue
                loc = _coords(None, None, proj.get("county"))
                if loc is None:
                    continue
                lat, lng, precision = loc
                features.append(
                    _point(
                        lat,
                        lng,
                        {
                            "layer": "ercot",
                            "stage": STAGE_QUEUED,
                            "id": f"ercot:{inr}",
                            "name": proj.get("interconnecting_entity")
                            or proj.get("project_name"),
                            "project_name": proj.get("project_name"),
                            "county": proj.get("county"),
                            "state": "TX",
                            "inr": inr,
                            "capacity_mw": proj.get("capacity_mw"),
                            "fuel": proj.get("fuel"),
                            "technology": proj.get("technology"),
                            "status": proj.get("status"),
                            "precision": precision,
                            "resolution_status": None,
                        },
                    )
                )

    counts = {
        "total": len(features),
        "exact": sum(1 for f in features if f["properties"]["precision"] == "exact"),
        "county": sum(1 for f in features if f["properties"]["precision"] == "county"),
    }
    by_stage: dict[str, int] = {}
    for f in features:
        s = f["properties"]["stage"]
        by_stage[s] = by_stage.get(s, 0) + 1

    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {"counts": counts, "by_stage": by_stage},
    }
