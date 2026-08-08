"""Deterministic stage inference from ERCOT milestone evidence.

The rule table is ordered latest-stage-first: the first rule whose predicate
fires wins, so a project with both an IA and a construction milestone reads as
`construction`, not `interconnection_agreement`.

Note on `fid`: the lifecycle has the stage but ERCOT publishes no FID field. The
closest real signal is "financial security / notice to proceed" alongside a
signed IA — money committed and notice given. `ercot_projects` has no such
column either, so in this codebase that evidence comes from a linked TCEQ air
permit (see `labels.permit_stage_evidence`). The mapping is an inference, not a
reported fact, and is flagged wherever it is used.

`construction` has no source at all today. It stays in the enum because the
lifecycle has it and dropping it would renumber every rank; it simply never gets
a weak label, which `label_quality` reports rather than hides.

Stdlib only. Nothing here imports pandas, sklearn, or the database.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class Stage(str, Enum):
    """Project lifecycle stage, ordered earliest to latest."""

    CONCEPT = "concept"
    FEL1 = "fel1"
    FEL2_PREFEED = "fel2_prefeed"
    FEED = "feed"
    INTERCONNECTION_AGREEMENT = "interconnection_agreement"
    FID = "fid"
    CONSTRUCTION = "construction"
    COD = "cod"

    @property
    def rank(self) -> int:
        """Ordinal position in the lifecycle. Higher means further along."""
        return list(Stage).index(self)


@dataclass
class StageInference:
    """Inferred stage for one project, with the evidence that produced it."""

    entity_id: str
    stage: Stage
    confidence: float
    justification: list[str] = field(default_factory=list)


# field -> value. Values are whatever the source wrote: a date string, "Y", or
# free text. Emptiness is the only thing the rules test uniformly.
Evidence = dict[str, str]

MILESTONE_FIELDS = (
    "screening_study_started",
    "screening_study_complete",
    "fis_requested",
    "fis_approved",
    "ia_signed",
    "financial_security_ntp",
    "construction_start",
    "construction_end",
    "approved_for_energization",
    "approved_for_synchronization",
    "projected_cod",
)

MIN_CONFIDENCE = 0.35
MAX_CONFIDENCE = 0.99


def _present(evidence: Evidence, *fields: str) -> list[str]:
    return [name for name in fields if evidence.get(name)]


def _affirmative(evidence: Evidence, name: str) -> bool:
    return evidence.get(name, "").strip().lower().startswith("y")


def _rule_cod(evidence: Evidence) -> list[str]:
    return _present(evidence, "approved_for_energization", "approved_for_synchronization")


def _rule_construction(evidence: Evidence) -> list[str]:
    return _present(evidence, "construction_start", "construction_end")


def _rule_fid(evidence: Evidence) -> list[str]:
    if _present(evidence, "ia_signed") and _affirmative(evidence, "financial_security_ntp"):
        return ["ia_signed", "financial_security_ntp"]
    return []


def _rule_ia(evidence: Evidence) -> list[str]:
    return _present(evidence, "ia_signed")


def _rule_feed(evidence: Evidence) -> list[str]:
    return _present(evidence, "fis_approved")


def _rule_prefeed(evidence: Evidence) -> list[str]:
    return _present(evidence, "fis_requested")


def _rule_fel1(evidence: Evidence) -> list[str]:
    return _present(evidence, "screening_study_started", "screening_study_complete")


RULES: tuple[tuple[Stage, Callable[[Evidence], list[str]], str], ...] = (
    (Stage.COD, _rule_cod, "energization or synchronization approved"),
    (Stage.CONSTRUCTION, _rule_construction, "construction milestone recorded"),
    (Stage.FID, _rule_fid, "IA signed with financial security / notice to proceed"),
    (Stage.INTERCONNECTION_AGREEMENT, _rule_ia, "interconnection agreement signed"),
    (Stage.FEED, _rule_feed, "full interconnection study approved"),
    (Stage.FEL2_PREFEED, _rule_prefeed, "full interconnection study requested"),
    (Stage.FEL1, _rule_fel1, "screening study underway or complete"),
)


def _confidence(evidence: Evidence, used: list[str]) -> float:
    """How much of the milestone picture is filled in, not how sure the rule is.

    A rule either fires or it does not. What varies is how much corroborating
    evidence sits around it, so confidence scales with the fraction of milestone
    fields populated plus a small bonus for a rule that needed two of them.
    """
    populated = sum(1 for name in MILESTONE_FIELDS if evidence.get(name))
    completeness = populated / len(MILESTONE_FIELDS)
    raw = 0.45 + 0.45 * completeness + 0.05 * min(len(used), 2)
    return round(max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, raw)), 4)


def infer_stage(milestones: Evidence, entity_id: str = "") -> StageInference:
    """First matching rule wins. Falls back to `concept` with the floor confidence."""
    evidence = {name: value for name, value in milestones.items() if value}

    for stage, predicate, description in RULES:
        used = predicate(evidence)
        if used:
            justification = [f"{name}={evidence[name]}" for name in used]
            justification.append(f"rule: {description}")
            return StageInference(
                entity_id=entity_id,
                stage=stage,
                confidence=_confidence(evidence, used),
                justification=justification,
            )

    return StageInference(
        entity_id=entity_id,
        stage=Stage.CONCEPT,
        confidence=MIN_CONFIDENCE,
        justification=[
            "appears in the queue with no milestone evidence",
            "rule: queue appearance only",
        ],
    )
