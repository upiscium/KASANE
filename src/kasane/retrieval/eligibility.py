"""Deterministic eligibility policy for retrieval candidates.

This module deliberately only answers whether a canonical record may enter a
retrieval candidate set.  Applicability and counterconditions are reserved for
an evaluator, so any non-empty value remains ineligible here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from kasane.vault.models import KnowledgeRecord, KnowledgeStatus

Freshness = Literal["fresh", "stale", "malformed"]
ExclusionReason = Literal[
    "scope", "lifecycle", "stale", "malformed_freshness", "applicability"
]
_EXCLUSION_REASONS: tuple[ExclusionReason, ...] = (
    "scope",
    "lifecycle",
    "stale",
    "malformed_freshness",
    "applicability",
)


@dataclass(frozen=True)
class EligibilityResult:
    """The complete, deterministic outcome for one retrieval record."""

    eligible: bool
    scope_specificity: int
    freshness: Freshness
    exclusion_reason: ExclusionReason | None


@dataclass(frozen=True)
class EligibilityCounts:
    """Eligibility diagnostics, in the same order as the Level 0 counters."""

    candidate_count: int
    excluded_scope: int
    excluded_lifecycle: int
    excluded_applicability: int
    excluded_stale: int
    malformed_freshness: int


class RetrievalEligibility:
    """Apply the Level 0 scope, lifecycle, freshness, and condition policy."""

    def evaluate(
        self,
        record: KnowledgeRecord,
        scope: Sequence[str],
        now: datetime,
    ) -> EligibilityResult:
        """Return the policy outcome without mutating the record or request."""

        if now.tzinfo is None:
            raise ValueError("retrieval clock must return a timezone-aware datetime")
        now = now.astimezone(UTC)
        specificity = scope_specificity(record.knowledge_ref, scope)
        if specificity < 0:
            return EligibilityResult(False, specificity, "fresh", "scope")
        if record.status is not KnowledgeStatus.CANONICAL:
            return EligibilityResult(False, specificity, "fresh", "lifecycle")
        freshness = record_freshness(record.stale_after, now)
        if freshness != "fresh":
            reason: ExclusionReason = (
                "malformed_freshness" if freshness == "malformed" else "stale"
            )
            return EligibilityResult(False, specificity, freshness, reason)
        if has_unevaluated_conditions(record):
            return EligibilityResult(False, specificity, freshness, "applicability")
        return EligibilityResult(True, specificity, freshness, None)

    def filter(
        self,
        records: list[KnowledgeRecord],
        scope: Sequence[str],
        now: datetime,
    ) -> tuple[list[KnowledgeRecord], EligibilityCounts]:
        eligible: list[KnowledgeRecord] = []
        excluded = {reason: 0 for reason in _EXCLUSION_REASONS}
        for record in records:
            result = self.evaluate(record, scope, now)
            if result.eligible:
                eligible.append(record)
            else:
                assert result.exclusion_reason is not None
                excluded[result.exclusion_reason] += 1
        return eligible, EligibilityCounts(
            len(records),
            excluded["scope"],
            excluded["lifecycle"],
            excluded["applicability"],
            excluded["stale"] + excluded["malformed_freshness"],
            excluded["malformed_freshness"],
        )


def scope_specificity(knowledge_ref: str, selectors: Sequence[str]) -> int:
    logical_path = knowledge_ref.removeprefix("vault://")
    matches = [
        len(selector.split("/"))
        for selector in selectors
        if logical_path == selector or logical_path.startswith(f"{selector}/")
    ]
    return max(matches, default=-1)


def record_freshness(stale_after: str | None, now: datetime) -> Freshness:
    if stale_after is None:
        return "fresh"
    try:
        if not isinstance(stale_after, str):
            return "malformed"
        parsed = datetime.fromisoformat(stale_after)
        if parsed.tzinfo is None:
            return "malformed"
        return "stale" if parsed.astimezone(UTC) <= now else "fresh"
    except (TypeError, ValueError, OverflowError):
        return "malformed"


def has_unevaluated_conditions(record: KnowledgeRecord) -> bool:
    applies_when_empty = record.applies_when is None or record.applies_when in ({}, [])
    counterconditions_empty = (
        record.counterconditions is None or record.counterconditions in ({}, [])
    )
    return not applies_when_empty or not counterconditions_empty
