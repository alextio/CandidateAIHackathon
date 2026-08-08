"""Parse ERCOT's `gim_study_phase` into milestone evidence.

What the column actually contains
---------------------------------
VERIFIED against the July 2026 GIS Report (1,238 projects, six monthly
snapshots). The live column is a comma-separated triple, never the long-form
prose an earlier draft of this codebase guessed at:

    SS Completed, FIS Started, No IA        652
    SS Completed, FIS Completed, IA         150
    SS Completed, FIS Started, IA           107
    SS Completed, FIS Completed, No IA       77
    SS Started, FIS Started, No IA           40
    (null)                                  212

Two things follow, and both were bugs before this module existed.

First, a substring scan over the whole string cannot read it. Every live value
contains "FIS", so a first-match-wins scan assigned every project the same
milestone and the same phase rank — `phase_rank` was the constant 2.0 for all
1,238 projects and `phase_velocity` was therefore 0.0 for every one of them.
Parsing per segment is what makes the column informative.

Second, `No IA` is a negation. Any scan that looks for "IA" anywhere reads the
642 projects that explicitly have *no* interconnection agreement as having one.
Segments are checked for negation before they count as evidence.

The long-form spellings ("Screening Study Complete", "Full Interconnection
Study Approved") are still recognised. They do not appear in the GIS Report
today, but they are the wording ERCOT's process documents use and cost nothing
to keep.

Stdlib only. Nothing here imports pandas, sklearn, numpy, or the database.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Milestone -> position on the lifecycle ladder. Ordered to match `Stage`:
# screening, then study, then agreement, then money committed, then in service.
PHASE_LADDER: tuple[tuple[str, int], ...] = (
    ("screening_study_started", 1),
    ("screening_study_complete", 2),
    ("fis_requested", 3),
    ("fis_approved", 4),
    ("ia_signed", 5),
    ("financial_security_ntp", 6),
    ("approved_for_energization", 7),
    ("approved_for_synchronization", 7),
)

_RANK_OF = dict(PHASE_LADDER)

# Which milestone family a segment is talking about. `\b` matters: without it
# the bare "ia" pattern fires inside "financial" and "ss" inside "assessment".
_SUBJECTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("energization", re.compile(r"energiz")),
    ("synchronization", re.compile(r"synchroniz")),
    ("security", re.compile(r"financial security|\bsecurity\b|\bntp\b")),
    ("fis", re.compile(r"full interconnection study|\bfis\b")),
    ("ia", re.compile(r"interconnection agreement|\bia\b")),
    ("screening", re.compile(r"screening study|\bss\b")),
)

# A segment that denies its own subject. "No IA" is the common one; the rest are
# defensive against wording drift.
_NEGATED = re.compile(r"\b(no|not|none|n/?a|pending|awaiting|tbd)\b")

# A segment that asserts the milestone finished rather than merely began.
_COMPLETE = re.compile(r"\b(complete|completed|approved|signed|executed|posted|done)\b")


def _segments(text: str) -> list[str]:
    """Split on commas and semicolons.

    Not on "/" — ERCOT writes "N/A" and "FIS/IA", and splitting there would
    turn one negation into two unqualified subjects.
    """
    return [part.strip() for part in re.split(r"[,;]", text) if part.strip()]


@lru_cache(maxsize=512)
def parse_phase(value: str | None) -> tuple[tuple[str, str], ...]:
    """Milestone evidence in one `gim_study_phase` value.

    Returns `(milestone, evidence_segment)` pairs rather than a dict so the
    result is hashable and can be cached — the column has six distinct values
    across the whole queue, so caching turns a per-row parse into six.

    A completed milestone also emits its own start: a project whose FIS is
    complete requested one, and leaving that implicit would understate how much
    of the milestone picture is filled in.
    """
    if not value:
        return ()

    text = str(value).strip().lower()
    if not text:
        return ()

    found: dict[str, str] = {}

    for segment in _segments(text):
        subject = next(
            (name for name, pattern in _SUBJECTS if pattern.search(segment)), None
        )
        if subject is None or _NEGATED.search(segment):
            continue

        complete = bool(_COMPLETE.search(segment))

        if subject == "screening":
            found.setdefault("screening_study_started", segment)
            if complete:
                found.setdefault("screening_study_complete", segment)
        elif subject == "fis":
            found.setdefault("fis_requested", segment)
            if complete:
                found.setdefault("fis_approved", segment)
        elif subject == "ia":
            found.setdefault("ia_signed", segment)
        elif subject == "security":
            found.setdefault("financial_security_ntp", segment)
        elif subject == "energization":
            found.setdefault("approved_for_energization", segment)
        elif subject == "synchronization":
            found.setdefault("approved_for_synchronization", segment)

    return tuple(sorted(found.items()))


def phase_milestones(value: object) -> dict[str, str]:
    """`parse_phase` as a plain dict, for callers that want to mutate it."""
    if value is None:
        return {}
    return dict(parse_phase(str(value)))


@lru_cache(maxsize=512)
def phase_rank_of(value: str | None) -> float | None:
    """Furthest milestone the phase attests, on the `PHASE_LADDER` scale.

    `None` when nothing is recognised — an unmatched value gets no rank rather
    than a guessed one, because a wrong ordering here silently inverts
    `phase_velocity` for every project it touches.
    """
    ranks = [_RANK_OF[name] for name, _ in parse_phase(value) if name in _RANK_OF]
    return float(max(ranks)) if ranks else None


__all__ = ["PHASE_LADDER", "parse_phase", "phase_milestones", "phase_rank_of"]
