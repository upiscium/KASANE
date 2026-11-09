"""Application-facing Vault Core service boundary."""

from __future__ import annotations

from pathlib import Path

from .json_codec import JsonValue
from .models import (
    EventType,
    KnowledgeDraft,
    KnowledgeEvent,
    KnowledgeRecord,
    KnowledgeStatus,
    VerificationOutcome,
)
from .repository import VaultRepository


class VaultService:
    """Thin policy-preserving facade over the repository implementation."""

    def __init__(self, repository: VaultRepository) -> None:
        self.repository = repository

    @classmethod
    def open(cls, database_path: str | Path | None = None) -> VaultService:
        return cls(VaultRepository(database_path))

    def create_candidate(
        self, draft: KnowledgeDraft, *, actor: str, metadata: JsonValue = None
    ) -> KnowledgeRecord:
        return self.repository.create_candidate(
            draft, actor=actor, event_metadata=metadata
        )

    def get_knowledge(self, knowledge_ref: str) -> KnowledgeRecord:
        return self.repository.get_knowledge(knowledge_ref)

    def list_knowledge(
        self,
        *,
        namespace: str | None = None,
        status: KnowledgeStatus | None = None,
    ) -> list[KnowledgeRecord]:
        return self.repository.list_knowledge(namespace=namespace, status=status)

    def update_candidate(
        self,
        draft: KnowledgeDraft,
        *,
        expected_revision: int,
        actor: str,
        metadata: JsonValue = None,
    ) -> KnowledgeRecord:
        return self.repository.update_candidate(
            draft,
            expected_revision=expected_revision,
            actor=actor,
            event_metadata=metadata,
        )

    def record_verification(
        self,
        knowledge_ref: str,
        *,
        expected_revision: int,
        actor: str,
        outcome: VerificationOutcome,
        verification: JsonValue,
        metadata: JsonValue = None,
    ) -> KnowledgeRecord:
        return self.repository.record_verification(
            knowledge_ref,
            expected_revision=expected_revision,
            actor=actor,
            outcome=outcome,
            verification=verification,
            event_metadata=metadata,
        )

    def transition_lifecycle(
        self,
        knowledge_ref: str,
        target: KnowledgeStatus,
        *,
        expected_revision: int,
        actor: str,
        metadata: JsonValue = None,
    ) -> KnowledgeRecord:
        return self.repository.transition_lifecycle(
            knowledge_ref,
            target,
            expected_revision=expected_revision,
            actor=actor,
            event_metadata=metadata,
        )

    def record_signal_event(
        self,
        knowledge_ref: str,
        event_type: EventType,
        *,
        expected_revision: int,
        actor: str,
        metadata: JsonValue = None,
    ) -> KnowledgeEvent:
        return self.repository.record_signal_event(
            knowledge_ref,
            event_type,
            expected_revision=expected_revision,
            actor=actor,
            metadata=metadata,
        )

    def list_events(self, knowledge_ref: str) -> list[KnowledgeEvent]:
        return self.repository.list_events(knowledge_ref)
