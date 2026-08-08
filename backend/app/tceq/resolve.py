"""Entity resolution: link TCEQ permits to ERCOT queue projects.

The TCEQ Central Registry carries no lat/long, so we match on normalized name +
county only. County acts as a gate (a match must share a county to score high),
and names are compared with a token-set Jaccard after stripping corporate
suffixes. Every TCEQ entity yields a `resolved_links` row: a confident hit is
`resolved`, a weak one is `review`, and no candidate is `unresolved` — nothing
is dropped, so a human can revisit the misses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import PermitRecord

# Confidence bands on the 0..1 score.
RESOLVE_THRESHOLD = 0.72
REVIEW_THRESHOLD = 0.45

# Corporate/entity noise stripped before comparing names.
_SUFFIXES = {
    "inc", "llc", "llp", "lp", "ltd", "co", "corp", "corporation", "company",
    "holdings", "energy", "power", "generation", "solar", "wind", "storage",
    "lc", "l", "p", "the", "of", "tx", "texas", "project", "farm", "plant",
    "station", "facility", "gen", "llc.", "us", "usa",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_name(name: str | None) -> str:
    """Lowercase, drop punctuation and corporate-noise tokens -> canonical form."""
    if not name:
        return ""
    tokens = _TOKEN_RE.findall(str(name).lower())
    kept = [t for t in tokens if t not in _SUFFIXES]
    return " ".join(kept or tokens)  # fall back to raw tokens if all were noise


def _token_set(name: str) -> frozenset[str]:
    return frozenset(name.split())


def name_similarity(a: str, b: str) -> float:
    """Token-set Jaccard similarity of two already-normalized names (0..1)."""
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


@dataclass(frozen=True)
class ErcotEntity:
    inr: str
    name: str          # original interconnecting_entity or project_name
    normalized: str
    county: str | None


@dataclass
class LinkResult:
    rn_number: str
    permit_no: str
    inr: str | None
    status: str          # resolved | review | unresolved
    score: float
    method: str
    tceq_name: str
    ercot_name: str | None
    county: str | None
    raw: dict = field(default_factory=dict)


# ERCOT candidates indexed by normalized county name (None for county-less rows).
ErcotIndex = dict[str | None, list[ErcotEntity]]


def build_ercot_index(rows: list[dict]) -> ErcotIndex:
    """Build a county-blocked index from ercot_projects rows.

    Each project contributes candidate names from both interconnecting_entity
    and project_name (so we can match on either side), bucketed by county. This
    lets resolution compare a permit only against ERCOT projects in the same
    county — since county is a gate, a cross-county pair could never resolve
    anyway — turning an O(permits × all-projects) scan into a per-county one.
    """
    index: ErcotIndex = {}
    for row in rows:
        inr = row.get("inr")
        if not inr:
            continue
        county = (row.get("county") or None)
        county_norm = county.strip().upper() if county else None
        for raw_name in (row.get("interconnecting_entity"), row.get("project_name")):
            norm = normalize_name(raw_name)
            if norm:
                index.setdefault(county_norm, []).append(
                    ErcotEntity(inr=inr, name=raw_name, normalized=norm, county=county_norm)
                )
    return index


def resolve_record(record: PermitRecord, index: ErcotIndex) -> LinkResult:
    """Find the best ERCOT match for one TCEQ permit record (county-blocked)."""
    tceq_county = record.county.strip().upper() if record.county else None
    # Compare against both the company and the site name; keep the stronger.
    tceq_names = [n for n in (record.company_name, record.entity_name) if n]
    tceq_norms = [(normalize_name(n), n) for n in tceq_names]
    tceq_norms = [(norm, orig) for norm, orig in tceq_norms if norm]

    # Only ERCOT projects in the permit's county are viable candidates. County-less
    # ERCOT rows (rare) are always considered but can only reach `review`.
    candidates = list(index.get(None, []))
    if tceq_county:
        candidates += index.get(tceq_county, [])

    best_score = 0.0
    best: ErcotEntity | None = None
    best_tceq_name = tceq_names[0] if tceq_names else ""
    county_matched = False

    for cand in candidates:
        for norm, orig in tceq_norms:
            sim = name_similarity(norm, cand.normalized)
            if sim == 0.0:
                continue
            same_county = bool(
                tceq_county and cand.county and tceq_county == cand.county
            )
            # County agreement is a strong prior; disagreement caps the score so
            # a same-name/different-county pair can only ever land in `review`.
            score = sim
            if same_county:
                score = min(1.0, sim + 0.25)
            else:
                score = sim * 0.6
            if score > best_score:
                best_score = score
                best = cand
                best_tceq_name = orig
                county_matched = same_county

    if best is None:
        status = "unresolved"
        method = "no_candidate"
    elif best_score >= RESOLVE_THRESHOLD:
        status = "resolved"
        method = "name+county" if county_matched else "name_only"
    elif best_score >= REVIEW_THRESHOLD:
        status = "review"
        method = "name+county" if county_matched else "name_only"
    else:
        status = "unresolved"
        method = "below_threshold"

    return LinkResult(
        rn_number=record.rn_number,
        permit_no=record.permit_no or "",
        inr=best.inr if (best and status != "unresolved") else None,
        status=status,
        score=round(best_score, 4),
        method=method,
        tceq_name=best_tceq_name,
        ercot_name=best.name if best else None,
        county=record.county,
        raw={"county_matched": county_matched} if best else {},
    )


def resolve_records(
    records: list[PermitRecord], ercot_rows: list[dict]
) -> list[LinkResult]:
    """Resolve many records against the ERCOT index, one link per (RN, permit)."""
    index = build_ercot_index(ercot_rows)
    seen: dict[tuple[str, str], LinkResult] = {}
    for record in records:
        key = (record.rn_number, record.permit_no or "")
        if key in seen:
            continue
        seen[key] = resolve_record(record, index)
    return list(seen.values())
