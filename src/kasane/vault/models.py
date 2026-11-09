"""Vault Core immutable service models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .json_codec import JsonValue


class KnowledgeStatus(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    CANONICAL = "canonical"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class VerificationOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    REJECTED = "rejected"


class EventType(StrEnum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    VERIFICATION_RECORDED = "VERIFICATION_RECORDED"
    VERIFIED = "VERIFIED"
    PROMOTED = "PROMOTED"
    SUPERSEDED = "SUPERSEDED"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"
    REVALIDATION_REQUESTED = "REVALIDATION_REQUESTED"


@dataclass(frozen=True)
class KnowledgeDraft:
    knowledge_ref: str
    knowledge_type: str
    title: str
    body: str
    stability: str | None = None
    tags: list[str] = field(default_factory=list)
    scope: JsonValue = field(default_factory=dict)
    applies_when: JsonValue = field(default_factory=dict)
    counterconditions: JsonValue = field(default_factory=list)
    sources: JsonValue = field(default_factory=list)
    provenance: JsonValue = field(default_factory=dict)
    generated: JsonValue = field(default_factory=dict)
    verification: JsonValue = field(default_factory=dict)
    stale_after: str | None = None


@dataclass(frozen=True)
class KnowledgeRecord:
    knowledge_ref: str
    namespace: str
    knowledge_path: str
    knowledge_type: str
    title: str
    body: str
    status: KnowledgeStatus
    stability: str | None
    tags: list[str]
    scope: JsonValue
    applies_when: JsonValue
    counterconditions: JsonValue
    sources: JsonValue
    provenance: JsonValue
    generated: JsonValue
    verification: JsonValue
    verification_outcome: VerificationOutcome | None
    stale_after: str | None
    created_at: str
    updated_at: str
    revision: int


@dataclass(frozen=True)
class KnowledgeEvent:
    event_id: str
    knowledge_ref: str
    revision: int
    actor: str
    timestamp: str
    event_type: EventType
    metadata: JsonValue
