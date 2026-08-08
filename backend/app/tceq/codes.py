"""NAICS allowlist + label/normalization helpers for TCEQ air permits.

The data-center-power thesis (see app/ercot/codes.py) is about generation that
could serve data-center load: dedicated gas plants, backup/quick-start engines,
storage, fuel cells. In the TCEQ Central Registry the closest lever is NAICS.
AIRNSR is dominated by upstream oil & gas (211111 compressor stations), so we
flag rows whose NAICS falls in the electric-power-generation family as
`on_thesis`; the entity-resolution join to ercot_projects can promote the rest.
"""
from __future__ import annotations

# Sentinel dates the Central Registry uses for "unknown/open" affiliation bounds.
SENTINEL_DATES: set[str] = {"1800-01-01", "3000-12-31"}

# NAICS 2211xx — Electric Power Generation, Transmission and Distribution.
# We keep the generation-side codes that align with the data-center-power thesis.
#   221111 Hydroelectric            221112 Fossil Fuel Electric Power
#   221113 Nuclear                  221114 Solar
#   221115 Wind                     221116 Geothermal
#   221117 Biomass                  221118 Other Electric Power Generation
#   221121 Electric Bulk Power Transmission and Control
#   221122 Electric Power Distribution
POWER_GEN_NAICS: set[str] = {
    "221111", "221112", "221113", "221114", "221115",
    "221116", "221117", "221118", "221121", "221122",
    # legacy 6-digit rollups still seen in older TCEQ rows
    "221110", "221120",
}


def parse_naics(indus_type_cd_name: str | None) -> tuple[str | None, str | None]:
    """Split TCEQ's "334413 - Semiconductor..." into (code, label)."""
    if not indus_type_cd_name:
        return None, None
    text = str(indus_type_cd_name).strip()
    if not text:
        return None, None
    if "-" in text:
        code, _, label = text.partition("-")
        code = code.strip()
        label = label.strip()
        return (code or None), (label or None)
    # Some rows carry only a code or only a label.
    if text.replace(".", "").isdigit():
        return text, None
    return None, text


def is_power_generation(naics_code: str | None) -> bool:
    """True if the NAICS code is in the electric-power-generation family."""
    if not naics_code:
        return False
    return str(naics_code).strip() in POWER_GEN_NAICS


def entity_status(reg_ent_status_txt: str | None) -> str:
    """Map TCEQ's status text to our active | inactive | unknown."""
    if not reg_ent_status_txt:
        return "unknown"
    s = str(reg_ent_status_txt).strip().upper()
    if s == "ACTIVE":
        return "active"
    if s in {"INACTIVE", "DELETED", "TERMINATED", "VOID"}:
        return "inactive"
    return "unknown"
