"""Fetch TCEQ air (NSR) permit affiliations from the Central Registry.

Source: the TCEQ "Central Registry Files" datasets on the Texas Open Data
Portal (data.texas.gov), which run on Socrata. TCEQ splits the state across
five regional datasets that share one schema; together they cover all of Texas.
They are public (no auth) and expose Socrata's SODA query API, so we filter to
``program_code = AIRNSR`` (air New Source Review) server-side and paginate.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

# Socrata resource base. Each regional dataset is `<BASE>/<four-by-four>.json`.
SODA_BASE = "https://data.texas.gov/resource"

# region label -> Socrata four-by-four id (dataset). Verified live 2026-08.
REGION_DATASETS: dict[str, str] = {
    "Central Texas": "msah-s2rv",       # Regions 9, 11, 13
    "North Texas": "5eqq-7nad",         # Regions 1, 2, 3, 8
    "Dallas/Fort Worth": "t34q-qzi3",   # Region 4
    "Coastal & East Texas": "tzyg-j7q4",  # Regions 5, 10, 12, 14
    "Border & Permian Basin": "9iad-hrn8",  # Regions 6, 7, 15, 16
}

# The air program we care about. AIROP (operating permits) / AIREI (emissions
# inventory) are siblings we deliberately leave out of the NSR "getting real" lens.
AIR_NSR_PROGRAM = "AIRNSR"

_PAGE_SIZE = 1000


@dataclass
class RegionRows:
    region: str
    dataset_id: str
    rows: list[dict]


def fetch_region(
    client: httpx.Client,
    region: str,
    dataset_id: str,
    *,
    program_code: str = AIR_NSR_PROGRAM,
    page_size: int = _PAGE_SIZE,
    max_rows: int | None = None,
) -> list[dict]:
    """Fetch all `program_code` rows for one regional dataset, paginated."""
    rows: list[dict] = []
    offset = 0
    while True:
        limit = page_size
        if max_rows is not None:
            remaining = max_rows - len(rows)
            if remaining <= 0:
                break
            limit = min(page_size, remaining)
        resp = client.get(
            f"{SODA_BASE}/{dataset_id}.json",
            params={
                "program_code": program_code,
                "$limit": limit,
                "$offset": offset,
                "$order": ":id",  # stable pagination order
            },
            timeout=60,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            break
    return rows


def fetch_air_nsr(
    client: httpx.Client,
    *,
    regions: list[str] | None = None,
    program_code: str = AIR_NSR_PROGRAM,
    max_rows_per_region: int | None = None,
) -> list[RegionRows]:
    """Fetch AIRNSR rows for the requested regions (default: all of Texas).

    Each raw row is returned untouched, grouped by region, for the parser to
    normalize. `max_rows_per_region` caps the pull (handy for smoke tests).
    """
    targets = regions or list(REGION_DATASETS)
    out: list[RegionRows] = []
    for region in targets:
        dataset_id = REGION_DATASETS[region]
        rows = fetch_region(
            client,
            region,
            dataset_id,
            program_code=program_code,
            max_rows=max_rows_per_region,
        )
        out.append(RegionRows(region=region, dataset_id=dataset_id, rows=rows))
    return out
