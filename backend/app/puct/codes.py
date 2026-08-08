"""Seed dockets, item-type labels, and filing-type -> event mapping for PUCT.

Source #3 tracks a curated set of large-load / interconnection dockets on the
PUCT Interchange (see SEED_DOCKETS) plus resolves the parties that file in them
against ERCOT/TCEQ. We deliberately do NOT crawl all dockets — high signal, low
noise. The milestone mapping turns the filing index (item-type code + free-text
description) into a *small* set of dated regulatory events; we surface only the
"getting real" milestones, not one event per filing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeedDocket:
    control_number: str
    label: str


# Curated large-load / interconnection dockets. Verified live on Interchange.
# These are the process/rulemaking dockets that define the large-load lane; as
# project-specific CCN / interconnection cases surface, add their control numbers
# here (or pass them to /discover/puct?dockets=...).
SEED_DOCKETS: list[SeedDocket] = [
    SeedDocket("58481", "Rulemaking to Implement Large Load Interconnection Standards (PURA §37.0561)"),
    SeedDocket("59142", "Review of ERCOT's Large Load Interconnection Process"),
    SeedDocket("55999", "ERCOT Large Load Interconnection Process"),
]

SEED_CONTROL_NUMBERS: list[str] = [d.control_number for d in SEED_DOCKETS]
SEED_LABELS: dict[str, str] = {d.control_number: d.label for d in SEED_DOCKETS}

# Interchange UtilityType facet. 'A' (all) is what the filings search uses for
# these dockets; the field is required on the server-rendered route.
DEFAULT_UTILITY_TYPE = "A"

# PUCT item-type codes seen on Interchange, mapped to human labels. Unknown codes
# fall back to the raw code so nothing is silently dropped.
ITEM_TYPE_LABELS: dict[str, str] = {
    "APP": "Application",
    "ORD": "Order",
    "PFD": "Proposal for Decision",
    "AGR": "Agreement",
    "COM": "Comments",
    "PC": "Public Comment",
    "PRJ": "Project / Rulemaking Document",
    "PL": "Proposal for Publication",
    "LTRS": "Letters",
    "CONF": "Confidential Filing",
    "MISC": "Miscellaneous",
    "ADMN": "Administrative",
    "MOT": "Motion",
    "NOT": "Notice",
    "TARF": "Tariff",
    "TEST": "Testimony",
}


def item_type_label(code: str | None) -> str | None:
    """Human label for a PUCT item-type code (falls back to the raw code)."""
    if not code:
        return None
    c = code.strip().upper()
    return ITEM_TYPE_LABELS.get(c, code.strip())


# Direct item-type code -> regulatory milestone event. These are the codes whose
# mere presence is a milestone regardless of wording.
_ITEM_TYPE_EVENTS: dict[str, str] = {
    "APP": "application_filed",
    "ORD": "order_issued",
    "AGR": "agreement_approved",
}

# Description keyword -> milestone, checked when the item-type code alone is not
# decisive. Ordered most-specific first; first match wins.
_DESCRIPTION_EVENTS: list[tuple[str, str]] = [
    ("request for control number", "docket_opened"),
    ("interconnection agreement", "agreement_approved"),
    ("standard generation interconnection agreement", "agreement_approved"),
    ("application for", "application_filed"),
    ("application to", "application_filed"),
    ("order on", "order_issued"),
    ("final order", "order_issued"),
    ("order no", "order_issued"),
    ("proposal for decision", "order_issued"),
]


def milestone_event_type(item_type: str | None, description: str | None) -> str | None:
    """Classify a filing into a milestone event_type, or None if it is not one.

    Returns None for routine, high-volume filings (comments, notices of
    participation, etc.) so events stay low-volume and high-confidence. The
    "docket_opened" milestone for a docket's first filing is handled by the
    parser, which has the whole docket in view.
    """
    code = (item_type or "").strip().upper()
    if code in _ITEM_TYPE_EVENTS:
        return _ITEM_TYPE_EVENTS[code]
    desc = (description or "").lower()
    for needle, event in _DESCRIPTION_EVENTS:
        if needle in desc:
            return event
    return None
