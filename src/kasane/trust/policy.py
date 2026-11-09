"""Deterministic tier policy over the Vault event log (see Issue #40).

The policy is a pure function: it reads a record's status and its events and
returns an assessment plus at most one next action. It never decides
``canonical``; only a human Approve does.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from kasane.vault.json_codec import JsonValue
from kasane.vault.models import (
    EventType,
    KnowledgeEvent,
    KnowledgeStatus,
    VerificationOutcome,
)

POLICY_ACTOR = "policy"
OPERATOR_ACTOR = "operator"
RESERVED_ACTORS = frozenset({POLICY_ACTOR, OPERATOR_ACTOR})
CONTENT_EVENTS = frozenset({EventType.CREATED, EventType.UPDATED})


class Tier(IntEnum):
    T0 = 0
    T1 = 1
    T2 = 2
    T3 = 3


class PolicyAction(StrEnum):
    NONE = "none"
    VERIFY_BY_CORROBORATION = "verify_by_corroboration"
    PROMOTE_VERIFIED = "promote_verified"
    REQUEST_REVALIDATION = "request_revalidation"
    DEPRECATE = "deprecate"
    ARCHIVE = "archive"


@dataclass(frozen=True)
class TrustPolicyConfig:
    """Tunable thresholds. These are configuration, not wire contract."""

    corroborations_for_verified: int = 2
    successes_for_approval: int = 2
    failures_to_retire: int = 2
    require_distinct_models: bool = False

    def __post_init__(self) -> None:
        for name in (
            "corroborations_for_verified",
            "successes_for_approval",
            "failures_to_retire",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")


DEFAULT_POLICY = TrustPolicyConfig()


@dataclass(frozen=True)
class Participant:
    """Independence key (actor, session) plus the model that produced it."""

    actor: str
    session: str
    model: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.actor, self.session)


@dataclass(frozen=True)
class TrustAssessment:
    status: KnowledgeStatus
    tier: Tier | None
    author: Participant | None
    corroborations: int
    independent_verifications: int
    successes: int
    failures: int
    revalidation_requested: bool
    approval_eligible: bool
    action: PolicyAction

    def summary(self) -> dict[str, JsonValue]:
        return {
            "status": self.status.value,
            "tier": None if self.tier is None else f"T{int(self.tier)}",
            "corroborations": self.corroborations,
            "independent_verifications": self.independent_verifications,
            "successes": self.successes,
            "failures": self.failures,
            "revalidation_requested": self.revalidation_requested,
            "approval_eligible": self.approval_eligible,
        }


def participant_from_metadata(actor: str, metadata: JsonValue) -> Participant | None:
    """Read the agent block written by the trust service, if present."""

    if not isinstance(metadata, Mapping):
        return None
    agent = metadata.get("agent")
    nested = metadata.get("metadata")
    if not isinstance(agent, Mapping) and isinstance(nested, Mapping):
        agent = nested.get("agent")  # verification events nest the agent block
    if not isinstance(agent, Mapping):
        return None
    session, model = agent.get("session"), agent.get("model")
    if not isinstance(session, str) or not isinstance(model, str):
        return None
    return Participant(actor=actor, session=session, model=model)


def evaluate(
    status: KnowledgeStatus,
    verification_outcome: VerificationOutcome | None,
    events: Sequence[KnowledgeEvent],
    config: TrustPolicyConfig | None = None,
) -> TrustAssessment:
    config = config or DEFAULT_POLICY
    window, author = _current_content_window(events)
    author_key = author.key if author else None

    corroborators: list[Participant] = []
    verifiers: list[Participant] = []
    successes: list[Participant] = []
    failures: list[Participant] = []
    revalidation_requested = False

    for event in window:
        if event.event_type is EventType.REVALIDATION_REQUESTED:
            revalidation_requested = True
            continue
        participant = participant_from_metadata(event.actor, event.metadata)
        if participant is None or participant.actor in RESERVED_ACTORS:
            continue
        independent = participant.key != author_key
        if event.event_type is EventType.CORROBORATED and independent:
            corroborators.append(participant)
        elif event.event_type is EventType.VERIFICATION_RECORDED:
            outcome = _metadata_str(event.metadata, "outcome")
            if outcome == VerificationOutcome.PASSED.value:
                if independent:
                    verifiers.append(participant)
            else:
                failures.append(participant)
        elif event.event_type is EventType.OUTCOME_REPORTED:
            outcome = _metadata_str(event.metadata, "outcome")
            if outcome == "success" and independent:
                successes.append(participant)
            elif outcome == "failure":
                failures.append(participant)

    count = _independence_counter(author, config)
    n_corroborations = count(corroborators)
    n_verifications = count(verifiers)
    n_successes = count(successes)
    # Failures are counted by distinct (actor, session) so one agent cannot
    # retire knowledge alone by repeating itself; the author may report too.
    n_failures = len({p.key for p in failures})

    tier: Tier | None
    action = PolicyAction.NONE
    approval_eligible = False

    if status is KnowledgeStatus.CANDIDATE:
        tier = Tier.T0
        if n_failures >= config.failures_to_retire:
            action = PolicyAction.ARCHIVE
        elif n_failures == 0:
            if (
                verification_outcome is VerificationOutcome.PASSED
                and n_verifications >= 1
            ):
                action = PolicyAction.PROMOTE_VERIFIED
            elif n_corroborations >= config.corroborations_for_verified:
                action = PolicyAction.VERIFY_BY_CORROBORATION
    elif status is KnowledgeStatus.VERIFIED:
        if n_failures >= config.failures_to_retire:
            action = PolicyAction.ARCHIVE
        approval_eligible = (
            n_failures == 0 and n_successes >= config.successes_for_approval
        )
        tier = Tier.T2 if approval_eligible else Tier.T1
    elif status is KnowledgeStatus.CANONICAL:
        tier = Tier.T3
        if n_failures >= config.failures_to_retire:
            action = PolicyAction.DEPRECATE
        elif n_failures >= 1 and not revalidation_requested:
            action = PolicyAction.REQUEST_REVALIDATION
    else:
        tier = None

    return TrustAssessment(
        status=status,
        tier=tier,
        author=author,
        corroborations=n_corroborations,
        independent_verifications=n_verifications,
        successes=n_successes,
        failures=n_failures,
        revalidation_requested=revalidation_requested,
        approval_eligible=approval_eligible,
        action=action,
    )


def _current_content_window(
    events: Sequence[KnowledgeEvent],
) -> tuple[Sequence[KnowledgeEvent], Participant | None]:
    """Events after the latest content change, plus that change's author."""

    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event.event_type in CONTENT_EVENTS:
            author = participant_from_metadata(event.actor, event.metadata)
            return events[index + 1 :], author
    return events, None


def _independence_counter(
    author: Participant | None, config: TrustPolicyConfig
) -> Callable[[Iterable[Participant]], int]:
    def count(participants: Iterable[Participant]) -> int:
        items = list(participants)
        if config.require_distinct_models:
            excluded = {author.model} if author else set()
            return len({p.model for p in items} - excluded)
        return len({p.key for p in items})

    return count


def _metadata_str(metadata: JsonValue, key: str) -> str | None:
    if isinstance(metadata, Mapping):
        value = metadata.get(key)
        if isinstance(value, str):
            return value
    return None
