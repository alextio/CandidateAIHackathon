"""Fuel and technology code -> human label maps.

Sourced from the "Acronyms" sheet of the ERCOT GIS Report.
"""
from __future__ import annotations

FUEL_LABELS: dict[str, str] = {
    "BIO": "Biomass",
    "COA": "Coal",
    "GAS": "Gas",
    "GEO": "Geothermal",
    "HYD": "Hydrogen",
    "NUC": "Nuclear",
    "OIL": "Fuel Oil",
    "OTH": "Other",
    "PET": "Petcoke",
    "SOL": "Solar",
    "WAT": "Water",
    "WIN": "Wind",
}

TECHNOLOGY_LABELS: dict[str, str] = {
    "BA": "Battery Energy Storage",
    "CC": "Combined-Cycle",
    "CE": "Compressed Air Energy Storage",
    "CP": "Concentrated Solar Power",
    "EN": "Energy Storage",
    "FC": "Fuel Cell",
    "GT": "Combustion (gas) Turbine, not part of a Combined-Cycle",
    "HY": "Hydroelectric Turbine",
    "IC": "Internal Combustion Engine (reciprocating)",
    "OT": "Other",
    "PV": "Photovoltaic Solar",
    "ST": "Steam Turbine (non Combined-Cycle)",
    "WT": "Wind Turbine",
}


# Technologies relevant to co-located / behind-the-meter data-center power.
# Used to narrow discovery to projects that could serve data-center load:
#   CC – Combined-Cycle: efficient dispatchable baseload gas; leading choice for
#        new dedicated/large gas plants co-located with data centers.
#   GT – Simple-cycle Gas Turbine: faster to permit/build, quick-to-power and
#        peaking/backup capacity that sidesteps ERCOT queue delays.
#   IC – Reciprocating Internal Combustion Engine: on-site backup/resiliency and
#        modular fast-start capacity at data-center campuses.
#   BA – Battery Energy Storage: load-smoothing for spiky demand, ride-through,
#        grid services; increasingly paired directly with data-center loads.
#   EN – Energy Storage: same role as BA (broader storage classification).
#   FC – Fuel Cell: growing niche for cleaner, resilient on-site power without
#        waiting on grid interconnection.
DATA_CENTER_TECHNOLOGIES: set[str] = {"CC", "GT", "IC", "BA", "EN", "FC"}

# The Inactive / Cancellation sheets carry no Technology column, only Fuel
# (as full words, not codes). These are the fuel equivalents of the technologies
# above, used to keep those sheets on-thesis:
#   Gas             -> CC / GT / IC
#   Battery Storage -> BA / EN
# (Fuel Cell has no distinct fuel word in these sheets and does not appear.)
DATA_CENTER_FUELS: set[str] = {"GAS", "BATTERY STORAGE"}


def is_data_center_technology(code: str | None) -> bool:
    if code is None:
        return False
    return str(code).strip().upper() in DATA_CENTER_TECHNOLOGIES


def is_data_center_fuel(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().upper() in DATA_CENTER_FUELS


def fuel_label(code: str | None) -> str | None:
    if code is None:
        return None
    return FUEL_LABELS.get(str(code).strip().upper(), str(code).strip())


def technology_label(code: str | None) -> str | None:
    if code is None:
        return None
    return TECHNOLOGY_LABELS.get(str(code).strip().upper(), str(code).strip())
