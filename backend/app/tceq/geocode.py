"""Attach exact coordinates to permit records via EPA FRS (Tier-2 geocoding).

Given parsed PermitRecords, look up each distinct RN in FRS once and stamp the
matching lat/long back onto every record for that RN. Records whose RN is not in
FRS are left un-geocoded; the /map endpoint falls back to the county centroid.
"""
from __future__ import annotations

import httpx

from ..geo import frs
from ..models import PermitRecord


def geocode_records(
    client: httpx.Client, records: list[PermitRecord]
) -> int:
    """Fill lat/long on records from FRS. Returns the count of records geocoded."""
    rns = [r.rn_number for r in records if r.rn_number]
    if not rns:
        return 0
    points = frs.geocode_rns(client, rns)
    geocoded = 0
    for rec in records:
        point = points.get(rec.rn_number)
        if point is None:
            continue
        rec.latitude = point.latitude
        rec.longitude = point.longitude
        rec.geo_accuracy_m = point.accuracy_m
        rec.geo_source = "epa_frs"
        geocoded += 1
    return geocoded
