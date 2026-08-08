"""Weak labels: turn the deterministic rules into training data.

The rules in `stages.py` read a `milestone_dates` dict. `ercot_projects` stores
the same facts as typed columns under different names, so this module adapts one
to the other and lets the rule table do the deciding.

What the ERCOT schema can and cannot label
------------------------------------------
Mapping the columns honestly turns up a real limit. Of the eight lifecycle
stages, `ercot_projects` alone supports six:

    concept                   no milestone evidence           yes
    fel1                      screening study                 yes, via gim_study_phase
    fel2_prefeed              full interconnection study      yes, via gim_study_phase
    feed                      study approved                  yes, via gim_study_phase
    interconnection_agreement ia_signed                       yes
    fid                       financial security + NTP        NO SUCH COLUMN
    construction              construction milestone          NO SUCH COLUMN
    cod                       energization / synchronization  yes

There is no `financial_security_ntp` and no construction date anywhere in the
schema, so a classifier trained on ERCOT alone can never predict those two.
Pretending otherwise — by, say, mapping "IA signed and a late COD" to
construction — would manufacture a label out of an assumption and then score
well against itself.

The honest fix is another source, and one is already in this database: a TCEQ
air permit is a real pre-construction signal, since combustion plant cannot be
built without one. `permit_stage_evidence` uses permits linked through
`resolved_links` to label the FID stage. `construction` still gets nothing, and
`label_quality` says so out loud rather than letting a silently absent class
look like a rare one.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .stages import MILESTONE_FIELDS, Stage, StageInference, infer_stage

# Below this a weak label is too thin to train on. The rule table's own floor is
# 0.35 (fired on nothing but a queue appearance); 0.45 keeps labels that had at
# least some corroborating milestone evidence.
MIN_LABEL_CONFIDENCE = 0.45

# gim_study_phase text -> the milestone field it stands in for. Matched as
# case-insensitive substrings, longest first, because the live values are
# decorated ("FIS - Approved", "Screening Study Complete").
#
# UNVERIFIED AGAINST LIVE DATA — the same caveat as momentum.PHASE_RANKS.
PHASE_MILESTONES: tuple[tuple[str, str], ...] = (
    ("full interconnection study approved", "fis_approved"),
    ("full interconnection study", "fis_requested"),
    ("screening study complete", "screening_study_complete"),
    ("screening study", "screening_study_started"),
    ("fis approved", "fis_approved"),
    ("fis complete", "fis_approved"),
    ("fis", "fis_requested"),
)

# Direct column -> milestone field, where only the names differ.
COLUMN_MILESTONES: tuple[tuple[str, str], ...] = (
    ("ia_signed", "ia_signed"),
    ("approved_for_energization", "approved_for_energization"),
    ("approved_for_synchronization", "approved_for_synchronization"),
    ("projected_cod", "projected_cod"),
)

ACTIVE_PERMIT_STATUSES = {"active", "a", "issued"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value).strip()


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def milestones_from_snapshot(snapshot: dict[str, Any]) -> dict[str, str]:
    """Map one `ercot_projects` row onto the milestone dict the rules expect."""
    milestones: dict[str, str] = {}

    for column, milestone in COLUMN_MILESTONES:
        value = _text(snapshot.get(column))
        if value:
            milestones[milestone] = value

    phase = _text(snapshot.get("gim_study_phase")).lower()
    if phase:
        for pattern, milestone in PHASE_MILESTONES:
            if pattern in phase:
                milestones.setdefault(milestone, _text(snapshot.get("gim_study_phase")))
                break

    return milestones


def is_withdrawn(snapshot: dict[str, Any]) -> bool:
    """Cancelled or inactive.

    Deliberately not a stage. Withdrawal is not a position on the lifecycle, and
    adding it as a ninth class would put a non-ordinal value on an ordinal
    scale — every ordinal metric would then be measuring partial nonsense.
    """
    return bool(
        _text(snapshot.get("cancel_date"))
        or _text(snapshot.get("inactive_date"))
        or _text(snapshot.get("status")).lower() in {"cancelled", "canceled", "inactive"}
    )


def permit_stage_evidence(permits: list[dict[str, Any]], as_of: date) -> str | None:
    """Stage evidence from linked TCEQ permits, for the stage ERCOT cannot label.

    An issued air permit is a genuine commitment signal: a developer does not
    complete air permitting for a project they have not decided to build. It is
    the closest thing to an observable FID in public data.

    Conservative on purpose — fires only on an active permit whose affiliation
    began at or before `as_of` and has not ended, so a pending application does
    not read as a commitment.
    """
    for permit in permits:
        status = _text(permit.get("entity_status")).lower()
        began = _as_date(permit.get("affiliation_begin_date"))
        ended = _as_date(permit.get("affiliation_end_date"))

        if not began or status not in ACTIVE_PERMIT_STATUSES:
            continue
        if began > as_of:
            continue
        if ended and ended <= as_of:
            continue

        return "financial_security_ntp"

    return None


@dataclass
class WeakLabel:
    """One training example produced by the rules rather than by a human."""

    entity_id: str
    inr: str
    as_of: date
    stage: Stage
    confidence: float
    rule: str
    withdrawn: bool = False
    justification: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether this label is strong enough to train on.

        Withdrawn projects are excluded outright: their last observed stage
        describes where they stopped, not where a live project of that shape
        would be, and including them teaches the model that stalling looks like
        progress.
        """
        return self.confidence >= MIN_LABEL_CONFIDENCE and not self.withdrawn


def weak_label(
    snapshot: dict[str, Any],
    *,
    entity_id: str,
    as_of: date,
    linked_permits: list[dict[str, Any]] | None = None,
) -> WeakLabel:
    """Label one project snapshot using the rule table."""
    milestones = milestones_from_snapshot(snapshot)

    permit_milestone = permit_stage_evidence(linked_permits or [], as_of)
    if permit_milestone:
        # The FID rule wants an affirmative value, not a date.
        milestones.setdefault(permit_milestone, "Y")

    inference: StageInference = infer_stage(milestones, entity_id=entity_id)

    rule = next(
        (
            line.removeprefix("rule: ")
            for line in inference.justification
            if line.startswith("rule: ")
        ),
        "unknown",
    )

    return WeakLabel(
        entity_id=entity_id,
        inr=_text(snapshot.get("inr")),
        as_of=as_of,
        stage=inference.stage,
        confidence=inference.confidence,
        rule=rule,
        withdrawn=is_withdrawn(snapshot),
        justification=list(inference.justification),
    )


@dataclass
class LabelQuality:
    """Diagnostics for a set of weak labels."""

    n_total: int
    n_usable: int
    n_withdrawn: int
    n_low_confidence: int
    by_stage: dict[str, int]
    by_rule: dict[str, int]
    missing_stages: list[str]

    @property
    def usable_fraction(self) -> float:
        return self.n_usable / self.n_total if self.n_total else 0.0

    def report(self) -> str:
        lines = [
            f"weak labels: {self.n_usable:,} usable of {self.n_total:,} "
            f"({self.usable_fraction:.1%})",
            f"  dropped: {self.n_withdrawn:,} withdrawn, "
            f"{self.n_low_confidence:,} below confidence {MIN_LABEL_CONFIDENCE}",
            "  by stage:",
        ]
        for stage, count in sorted(self.by_stage.items(), key=lambda item: -item[1]):
            lines.append(f"    {stage:28} {count:>7,}")
        lines.append("  by rule:")
        for rule, count in sorted(self.by_rule.items(), key=lambda item: -item[1]):
            lines.append(f"    {rule[:44]:44} {count:>7,}")
        if self.missing_stages:
            lines.append(
                "  NO LABELS for: "
                + ", ".join(self.missing_stages)
                + "  — these classes cannot be predicted"
            )
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form, for the API response."""
        return {
            "n_total": self.n_total,
            "n_usable": self.n_usable,
            "n_withdrawn": self.n_withdrawn,
            "n_low_confidence": self.n_low_confidence,
            "usable_fraction": self.usable_fraction,
            "by_stage": self.by_stage,
            "by_rule": self.by_rule,
            "missing_stages": self.missing_stages,
        }


def label_quality(labels: list[WeakLabel]) -> LabelQuality:
    """Summarise weak-label coverage, skew, and which classes are unreachable.

    Worth reading before every training run. The rules are correlated labelling
    functions over a shared set of milestone fields, so a class that looks well
    represented may be one rule firing repeatedly rather than several
    independent sources agreeing.
    """
    usable = [label for label in labels if label.usable]

    by_stage = Counter(label.stage.value for label in usable)
    by_rule = Counter(label.rule for label in usable)
    missing = [stage.value for stage in Stage if by_stage.get(stage.value, 0) == 0]

    return LabelQuality(
        n_total=len(labels),
        n_usable=len(usable),
        n_withdrawn=sum(1 for label in labels if label.withdrawn),
        n_low_confidence=sum(
            1
            for label in labels
            if not label.withdrawn and label.confidence < MIN_LABEL_CONFIDENCE
        ),
        by_stage=dict(by_stage),
        by_rule=dict(by_rule),
        missing_stages=missing,
    )


__all__ = [
    "MILESTONE_FIELDS",
    "MIN_LABEL_CONFIDENCE",
    "LabelQuality",
    "WeakLabel",
    "is_withdrawn",
    "label_quality",
    "milestones_from_snapshot",
    "permit_stage_evidence",
    "weak_label",
]
