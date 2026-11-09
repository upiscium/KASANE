"""Agent intake, trust signals, and human approval over Vault Core (Issue #39)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kasane.contracts import validate_contract
from kasane.vault.errors import KnowledgeNotFoundError, StaleRevisionError
from kasane.vault.identity import parse_knowledge_ref
from kasane.vault.json_codec import JsonValue
from kasane.vault.models import (
    EventType,
    KnowledgeDraft,
    KnowledgeRecord,
    KnowledgeStatus,
    VerificationOutcome,
)
from kasane.vault.repository import VaultRepository

from .policy import (
    DEFAULT_POLICY,
    OPERATOR_ACTOR,
    POLICY_ACTOR,
    RESERVED_ACTORS,
    PolicyAction,
    TrustAssessment,
    TrustPolicyConfig,
    evaluate,
)

PROPOSAL_SCHEMA = "knowledge-proposal.schema.json"
SIGNAL_SCHEMA = "knowledge-signal.schema.json"
DEFAULT_MAX_BODY_BYTES = 24_000
_MAX_POLICY_STEPS = 4


class TrustRejectedError(ValueError):
    """A write was refused; the Vault is unchanged."""


@dataclass(frozen=True)
class AgentContext:
    """Who is writing. Comes from server configuration, never from payloads."""

    actor: str
    session: str
    model: str

    def __post_init__(self) -> None:
        for name in ("actor", "session", "model"):
            value = getattr(self, name)
            if not value.strip() or len(value) > 256:
                raise ValueError(f"agent {name} must be 1-256 non-blank characters")
        if self.actor in RESERVED_ACTORS:
            raise ValueError(f"actor {self.actor!r} is reserved")

    def metadata(self, **extra: JsonValue) -> dict[str, JsonValue]:
        return {"agent": {"session": self.session, "model": self.model}, **extra}


@dataclass(frozen=True)
class TrustResult:
    record: KnowledgeRecord
    assessment: TrustAssessment

    def summary(self) -> dict[str, JsonValue]:
        return {
            "knowledge_ref": self.record.knowledge_ref,
            "revision": self.record.revision,
            **self.assessment.summary(),
        }


class TrustService:
    def __init__(
        self,
        repository: VaultRepository,
        config: TrustPolicyConfig | None = None,
        *,
        write_scopes: Sequence[str] | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self.repository = repository
        self.config = config or DEFAULT_POLICY
        self.write_scopes = tuple(write_scopes) if write_scopes else None
        self.max_body_bytes = max_body_bytes

    # Agent operations -------------------------------------------------

    def propose(self, proposal: Mapping[str, Any], agent: AgentContext) -> TrustResult:
        validate_contract(PROPOSAL_SCHEMA, proposal)
        ref = str(proposal["knowledge_ref"])
        self._require_writable(ref)
        body = str(proposal["body"])
        if len(body.encode("utf-8")) > self.max_body_bytes:
            raise TrustRejectedError(f"body exceeds {self.max_body_bytes} UTF-8 bytes")
        stale_after = proposal.get("stale_after")
        if stale_after is not None:
            _require_aware_timestamp(str(stale_after))
        if self._exists(ref):
            raise TrustRejectedError(
                f"{ref} already exists; corroborate it instead of re-proposing"
            )
        draft = KnowledgeDraft(
            knowledge_ref=ref,
            knowledge_type=str(proposal["knowledge_type"]),
            title=str(proposal["title"]),
            body=body,
            tags=list(proposal.get("tags", [])),
            applies_when=proposal.get("applies_when", {}),
            counterconditions=proposal.get("counterconditions", []),
            sources=list(proposal["sources"]),
            provenance={"origin": "agent", "agent": agent.metadata()["agent"]},
            generated={"by": "agent"},
            stale_after=None if stale_after is None else str(stale_after),
        )
        try:
            self.repository.create_candidate(
                draft, actor=agent.actor, event_metadata=agent.metadata()
            )
        except sqlite3.IntegrityError as error:  # concurrent proposal won the race
            raise TrustRejectedError(f"{ref} already exists") from error
        return self.assess(ref)

    def signal(self, signal: Mapping[str, Any], agent: AgentContext) -> TrustResult:
        validate_contract(SIGNAL_SCHEMA, signal)
        ref = str(signal["knowledge_ref"])
        self._require_writable(ref)
        record = self._get(ref)
        if record.status in (KnowledgeStatus.DEPRECATED, KnowledgeStatus.ARCHIVED):
            raise TrustRejectedError(f"{ref} is {record.status.value}")
        kind = signal["kind"]
        note = signal.get("note")
        if kind == "verification":
            self._verify(
                record, agent, VerificationOutcome(signal["verification"]), note
            )
        else:
            event_type = (
                EventType.CORROBORATED
                if kind == "corroboration"
                else EventType.OUTCOME_REPORTED
            )
            extra: dict[str, JsonValue] = {}
            if note is not None:
                extra["note"] = note
            if kind == "outcome":
                extra["outcome"] = signal["outcome"]
            self.repository.record_signal_event(
                ref,
                event_type,
                expected_revision=record.revision,
                actor=agent.actor,
                metadata=agent.metadata(**extra),
            )
        self._apply_policy(ref)
        return self.assess(ref)

    # Read side ---------------------------------------------------------

    def assess(self, knowledge_ref: str) -> TrustResult:
        record = self._get(knowledge_ref)
        assessment = evaluate(
            record.status,
            record.verification_outcome,
            self.repository.list_events(knowledge_ref),
            self.config,
        )
        return TrustResult(record, assessment)

    def approval_queue(self) -> list[TrustResult]:
        results = [
            self.assess(record.knowledge_ref)
            for record in self.repository.list_knowledge()
            if record.status is KnowledgeStatus.VERIFIED
        ]
        return [result for result in results if result.assessment.approval_eligible]

    # Human operations (approval queue) --------------------------------

    def approve(self, knowledge_ref: str, *, note: str | None = None) -> TrustResult:
        current = self.assess(knowledge_ref)
        if not current.assessment.approval_eligible:
            raise TrustRejectedError(
                f"{knowledge_ref} is not approval-eligible (T2 required)"
            )
        self.repository.transition_lifecycle(
            knowledge_ref,
            KnowledgeStatus.CANONICAL,
            expected_revision=current.record.revision,
            actor=OPERATOR_ACTOR,
            event_metadata={"decision": "approve", "note": note},
        )
        return self.assess(knowledge_ref)

    def reject(self, knowledge_ref: str, *, reason: str) -> TrustResult:
        if not reason.strip():
            raise TrustRejectedError("reject requires a reason")
        record = self._get(knowledge_ref)
        if record.status not in (KnowledgeStatus.CANDIDATE, KnowledgeStatus.VERIFIED):
            raise TrustRejectedError(f"cannot reject {record.status.value} knowledge")
        self.repository.transition_lifecycle(
            knowledge_ref,
            KnowledgeStatus.ARCHIVED,
            expected_revision=record.revision,
            actor=OPERATOR_ACTOR,
            event_metadata={"decision": "reject", "reason": reason},
        )
        return self.assess(knowledge_ref)

    # Internals ---------------------------------------------------------

    def _verify(
        self,
        record: KnowledgeRecord,
        agent: AgentContext,
        outcome: VerificationOutcome,
        note: Any,
    ) -> None:
        if record.status is not KnowledgeStatus.CANDIDATE:
            raise TrustRejectedError(
                "only candidates take verifications; report outcomes instead"
            )
        author = self.assess(record.knowledge_ref).assessment.author
        if author is not None and (agent.actor, agent.session) == author.key:
            raise TrustRejectedError("self-verification is not allowed")
        self.repository.record_verification(
            record.knowledge_ref,
            expected_revision=record.revision,
            actor=agent.actor,
            outcome=outcome,
            verification={"basis": "agent", "note": note},
            event_metadata=agent.metadata(),
        )

    def _apply_policy(self, knowledge_ref: str) -> None:
        for _ in range(_MAX_POLICY_STEPS):
            current = self.assess(knowledge_ref)
            action = current.assessment.action
            if action is PolicyAction.NONE:
                return
            try:
                self._perform(current, action)
            except StaleRevisionError:
                continue  # another writer moved first; re-evaluate
        raise RuntimeError(f"trust policy did not settle for {knowledge_ref}")

    def _perform(self, current: TrustResult, action: PolicyAction) -> None:
        record, assessment = current.record, current.assessment
        ref, revision = record.knowledge_ref, record.revision
        evidence: dict[str, JsonValue] = {
            "policy": action.value,
            "assessment": dict(assessment.summary()),
        }
        if action is PolicyAction.VERIFY_BY_CORROBORATION:
            self.repository.record_verification(
                ref,
                expected_revision=revision,
                actor=POLICY_ACTOR,
                outcome=VerificationOutcome.PASSED,
                verification={"basis": "corroboration", **evidence},
            )
            record = self._get(ref)
            revision = record.revision
            action = PolicyAction.PROMOTE_VERIFIED
        if action is PolicyAction.PROMOTE_VERIFIED:
            self.repository.transition_lifecycle(
                ref,
                KnowledgeStatus.VERIFIED,
                expected_revision=revision,
                actor=POLICY_ACTOR,
                event_metadata=evidence,
            )
        elif action is PolicyAction.REQUEST_REVALIDATION:
            self.repository.record_signal_event(
                ref,
                EventType.REVALIDATION_REQUESTED,
                expected_revision=revision,
                actor=POLICY_ACTOR,
                metadata=evidence,
            )
        elif action is PolicyAction.DEMOTE:
            self.repository.transition_lifecycle(
                ref,
                KnowledgeStatus.CANDIDATE,
                expected_revision=revision,
                actor=POLICY_ACTOR,
                event_metadata=evidence,
            )
        elif action is PolicyAction.DEPRECATE:
            self.repository.transition_lifecycle(
                ref,
                KnowledgeStatus.DEPRECATED,
                expected_revision=revision,
                actor=POLICY_ACTOR,
                event_metadata=evidence,
            )
        elif action is PolicyAction.ARCHIVE:
            self.repository.transition_lifecycle(
                ref,
                KnowledgeStatus.ARCHIVED,
                expected_revision=revision,
                actor=POLICY_ACTOR,
                event_metadata=evidence,
            )

    def _require_writable(self, knowledge_ref: str) -> None:
        identity = parse_knowledge_ref(knowledge_ref)
        if self.write_scopes is None:
            return
        if not any(
            identity.namespace == scope or identity.namespace.startswith(scope + "/")
            for scope in self.write_scopes
        ):
            raise TrustRejectedError(
                f"{knowledge_ref} is outside the allowed write scopes"
            )

    def _get(self, knowledge_ref: str) -> KnowledgeRecord:
        try:
            return self.repository.get_knowledge(knowledge_ref)
        except KnowledgeNotFoundError as error:
            raise TrustRejectedError(f"unknown knowledge: {knowledge_ref}") from error

    def _exists(self, knowledge_ref: str) -> bool:
        try:
            self.repository.get_knowledge(knowledge_ref)
        except KnowledgeNotFoundError:
            return False
        return True


def _require_aware_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise TrustRejectedError(f"stale_after is not ISO 8601: {value}") from error
    if parsed.tzinfo is None:
        raise TrustRejectedError("stale_after must include a timezone")
