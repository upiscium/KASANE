"""Transactional Vault Core v0.1."""

from .errors import (
    InvalidKnowledgeIdentityError,
    InvalidLifecycleTransitionError,
    InvalidMetadataError,
    KnowledgeNotFoundError,
    StaleRevisionError,
    VaultError,
)
from .models import (
    EventType,
    KnowledgeDraft,
    KnowledgeEvent,
    KnowledgeRecord,
    KnowledgeStatus,
    VerificationOutcome,
)
from .repository import VaultRepository
from .service import VaultService

__all__ = [
    "EventType",
    "InvalidKnowledgeIdentityError",
    "InvalidLifecycleTransitionError",
    "InvalidMetadataError",
    "KnowledgeDraft",
    "KnowledgeEvent",
    "KnowledgeNotFoundError",
    "KnowledgeRecord",
    "KnowledgeStatus",
    "StaleRevisionError",
    "VaultError",
    "VaultRepository",
    "VaultService",
    "VerificationOutcome",
]
