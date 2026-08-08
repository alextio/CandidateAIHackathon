"""Entity resolution: link PUCT filing parties to ERCOT and TCEQ entities.

We reuse the TCEQ resolver's normalization and token-set scoring (importing
``normalize_name`` / ``name_similarity`` / ``build_ercot_index``) so the three
sources speak one identity language. The difference: PUCT filings carry **no
county**, so there is no county gate — matching is name-only. We therefore band
more conservatively (an exact-ish normalized name is required to auto-resolve),
and we match against both the ERCOT queue and TCEQ company names, keeping the
stronger. Every distinct (party, docket) yields a link row; misses are kept as
``unresolved`` so a human can revisit.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import FilingRecord
# Share the identity language with Source #2 — do not fork these helpers.
from ..tceq.resolve import (
    ErcotEntity,
    build_ercot_index,
    name_similarity,
    normalize_name,
)

# Name-only confidence bands (higher than TCEQ's, which gets a county prior).
RESOLVE_THRESHOLD = 0.80
REVIEW_THRESHOLD = 0.50


@dataclass(frozen=True)
class TceqEntity:
    rn_number: str
    name: str
    normalized: str


@dataclass
class PuctLinkResult:
    control_number: str
    party: str            # the PUCT filing-party name (left side)
    party_slug: str       # normalized party name, used as the link's rn_number
    status: str           # resolved | review | unresolved
    score: float
    method: str           # name_only:ercot | name_only:tceq | no_candidate | below_threshold
    matched_source: str | None  # ercot | tceq | None
    inr: str | None       # matched ercot_projects.inr (None unless ercot won)
    tceq_rn: str | None   # matched tceq_permits.rn_number (None unless tceq won)
    matched_name: str | None
    raw: dict = field(default_factory=dict)


def build_tceq_index(rows: list[dict]) -> list[TceqEntity]:
    """Build a resolvable index from tceq_permits rows (company + site names)."""
    index: list[TceqEntity] = []
    for row in rows:
        rn = row.get("rn_number")
        if not rn:
            continue
        for raw_name in (row.get("company_name"), row.get("entity_name")):
            norm = normalize_name(raw_name)
            if norm:
                index.append(TceqEntity(rn_number=rn, name=raw_name, normalized=norm))
    return index


def _best(norm: str, index_norms: list[tuple[str, object]]) -> tuple[float, object | None]:
    best_score = 0.0
    best_obj: object | None = None
    for cand_norm, obj in index_norms:
        sim = name_similarity(norm, cand_norm)
        if sim > best_score:
            best_score = sim
            best_obj = obj
    return best_score, best_obj


def resolve_party(
    control_number: str,
    party: str,
    ercot_index: list[ErcotEntity],
    tceq_index: list[TceqEntity],
) -> PuctLinkResult:
    """Find the best ERCOT/TCEQ match for one filing party (name-only)."""
    slug = normalize_name(party)
    result_base = dict(control_number=control_number, party=party, party_slug=slug or party.lower())

    if not slug:
        return PuctLinkResult(
            status="unresolved", score=0.0, method="no_name",
            matched_source=None, inr=None, tceq_rn=None, matched_name=None,
            **result_base,
        )

    e_score, e_obj = _best(slug, [(c.normalized, c) for c in ercot_index])
    t_score, t_obj = _best(slug, [(c.normalized, c) for c in tceq_index])

    if e_score >= t_score:
        best_source, best_score, best_obj = "ercot", e_score, e_obj
    else:
        best_source, best_score, best_obj = "tceq", t_score, t_obj

    if best_obj is None or best_score < REVIEW_THRESHOLD:
        return PuctLinkResult(
            status="unresolved",
            score=round(best_score, 4),
            method="below_threshold" if best_obj is not None else "no_candidate",
            matched_source=None, inr=None, tceq_rn=None, matched_name=None,
            **result_base,
        )

    status = "resolved" if best_score >= RESOLVE_THRESHOLD else "review"
    if best_source == "ercot":
        cand: ErcotEntity = best_obj  # type: ignore[assignment]
        return PuctLinkResult(
            status=status, score=round(best_score, 4), method="name_only:ercot",
            matched_source="ercot", inr=cand.inr, tceq_rn=None, matched_name=cand.name,
            raw={"ercot_inr": cand.inr}, **result_base,
        )
    cand_t: TceqEntity = best_obj  # type: ignore[assignment]
    return PuctLinkResult(
        status=status, score=round(best_score, 4), method="name_only:tceq",
        matched_source="tceq", inr=None, tceq_rn=cand_t.rn_number, matched_name=cand_t.name,
        raw={"tceq_rn": cand_t.rn_number}, **result_base,
    )


def resolve_records(
    records: list[FilingRecord],
    ercot_rows: list[dict],
    tceq_rows: list[dict],
) -> list[PuctLinkResult]:
    """Resolve every distinct (party, docket) across the parsed filings.

    One link per (control_number, normalized party) — a party that files many
    times in a docket resolves once; a party active in several dockets yields one
    link per docket, so the map can bump every project it touches.
    """
    # build_ercot_index returns a county-blocked dict (county gate is for TCEQ);
    # PUCT filings carry no county, so flatten every bucket into one candidate list.
    ercot_index = [e for bucket in build_ercot_index(ercot_rows).values() for e in bucket]
    tceq_index = build_tceq_index(tceq_rows)

    seen: dict[tuple[str, str], PuctLinkResult] = {}
    for rec in records:
        if not rec.filing_party:
            continue
        slug = normalize_name(rec.filing_party) or rec.filing_party.lower()
        key = (rec.control_number, slug)
        if key in seen:
            continue
        seen[key] = resolve_party(rec.control_number, rec.filing_party, ercot_index, tceq_index)
    return list(seen.values())
