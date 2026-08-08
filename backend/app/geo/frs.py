"""Tier-2 geocoding: TCEQ RN number -> exact lat/long via EPA's FRS.

The EPA Facility Registry Service (FRS) publishes an ArcGIS FeatureServer whose
points carry lat/long and a cross-reference to state program IDs. TCEQ Central
Registry entities appear with ``PGM_SYS_ACRNM = 'TX-TCEQ ACR'`` and
``PGM_SYS_ID = <RN number>``, so we can look up coordinates for the RNs we
already store. Not every RN is present (newer/un-geocoded entities are missing);
callers fall back to the county centroid for those.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

FRS_QUERY_URL = (
    "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/"
    "FRS_INTERESTS/FeatureServer/0/query"
)
TCEQ_ACRONYM = "TX-TCEQ ACR"

_BATCH = 80  # RNs per request; keeps the IN(...) clause well within limits


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float
    accuracy_m: int | None = None


def _quote_in(values: list[str]) -> str:
    escaped = [v.replace("'", "''") for v in values]
    return ",".join(f"'{v}'" for v in escaped)


def _valid(lat, lng) -> bool:
    # FRS uses NAD83; drop nulls and obvious 0/placeholder coordinates.
    if lat in (None, 0) or lng in (None, 0):
        return False
    # Texas bounding box sanity check.
    return 25.0 <= float(lat) <= 37.0 and -107.0 <= float(lng) <= -93.0


def geocode_rns(
    client: httpx.Client, rns: list[str], *, batch_size: int = _BATCH
) -> dict[str, GeoPoint]:
    """Look up coordinates for TCEQ RN numbers. Returns {rn: GeoPoint} for hits."""
    unique = sorted({rn for rn in rns if rn})
    out: dict[str, GeoPoint] = {}
    for start in range(0, len(unique), batch_size):
        chunk = unique[start : start + batch_size]
        where = (
            f"PGM_SYS_ACRNM='{TCEQ_ACRONYM}' AND "
            f"PGM_SYS_ID IN ({_quote_in(chunk)})"
        )
        resp = client.post(
            FRS_QUERY_URL,
            data={
                "where": where,
                "outFields": "PGM_SYS_ID,LATITUDE83,LONGITUDE83,ACCURACY_VALUE",
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()
        for feat in payload.get("features", []):
            attrs = feat.get("attributes", {})
            rn = attrs.get("PGM_SYS_ID")
            lat, lng = attrs.get("LATITUDE83"), attrs.get("LONGITUDE83")
            if not rn or rn in out or not _valid(lat, lng):
                continue
            acc = attrs.get("ACCURACY_VALUE")
            out[rn] = GeoPoint(
                latitude=round(float(lat), 6),
                longitude=round(float(lng), 6),
                accuracy_m=int(acc) if isinstance(acc, (int, float)) else None,
            )
    return out
