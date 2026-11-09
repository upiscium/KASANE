"""Eligibility-gated semantic candidate generation.

This is an internal expansion path.  It deliberately stops at scored,
traceable candidates and does not combine them with lexical results.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from kasane.vault.models import KnowledgeRecord

from .eligibility import CANONICAL_TIER, RetrievalEligibility, TierResolver
from .embeddings import (
    SEMANTIC_DOCUMENT_REPRESENTATION_VERSION,
    EmbeddingProviderIdentity,
    EmbeddingValidationError,
    validate_embedding_provider,
)
from .semantic_index import DerivedSemanticIndex, SemanticIndexSyncResult

MAX_SEMANTIC_CANDIDATES = 32
# This is an explicit, deterministic provider-input bound.  Records are sorted
# by public reference before this cap is applied, so corpus insertion order
# cannot change which documents reach an embedding provider.
MAX_SEMANTIC_RECORDS = 256


@dataclass(frozen=True)
class SemanticCandidate:
    """A semantic hit with enough identity to be safely audited."""

    knowledge_ref: str
    semantic_score: float
    provider_id: str
    model_id: str
    embedding_dimension: int
    representation_version: str = SEMANTIC_DOCUMENT_REPRESENTATION_VERSION

    @property
    def score(self) -> float:
        """Convenient internal alias for consumers handling scored candidates."""
        return self.semantic_score

    @property
    def provider_identity(self) -> EmbeddingProviderIdentity:
        return EmbeddingProviderIdentity(
            self.provider_id, self.model_id, self.embedding_dimension
        )


@dataclass(frozen=True)
class SemanticCandidateResult:
    candidates: tuple[SemanticCandidate, ...]
    synchronization: SemanticIndexSyncResult
    records_seen: int = 0
    records_truncated: int = 0
    record_cap: int = MAX_SEMANTIC_RECORDS


def utc_now() -> datetime:
    return datetime.now(UTC)


class SemanticCandidateService:
    """Synchronize and query semantic state behind the Level 0 hard gate."""

    def __init__(self, index: DerivedSemanticIndex) -> None:
        self.index = index
        self._eligibility = RetrievalEligibility()

    def generate(
        self,
        records: Iterable[KnowledgeRecord],
        query: str,
        scope: Sequence[str],
        now: datetime | None = None,
        *,
        min_trust: int = CANONICAL_TIER,
        tier_of: TierResolver | None = None,
    ) -> SemanticCandidateResult:
        source = list(records)
        # Only records eligible for *this* request reach the semantic index;
        # ineligible ones would otherwise occupy candidate slots (#41).
        eligible, _ = self._eligibility.filter(
            source, scope, now or utc_now(), min_trust, tier_of
        )
        bounded = sorted(eligible, key=lambda record: record.knowledge_ref)
        truncated = max(0, len(bounded) - MAX_SEMANTIC_RECORDS)
        bounded = bounded[:MAX_SEMANTIC_RECORDS]
        # Ineligible records never reach semantic_document/embed_documents.
        synchronization = self.index.synchronize(bounded)
        try:
            hits = self.index.search(query, (r.knowledge_ref for r in bounded))
        except (
            sqlite3.DatabaseError,
            EmbeddingValidationError,
            RuntimeError,
            ValueError,
        ):
            # Synchronization and search are separate filesystem operations.  A
            # derived file may disappear, become corrupt, or change vector
            # space in between them.  Rebuild only from the already gated set,
            # then retry exactly once.
            synchronization = self.index.rebuild(bounded)
            hits = self.index.search(query, (r.knowledge_ref for r in bounded))
        # Search can produce one hit per eligible record.  Bound the provider
        # result here (rather than rejecting an otherwise valid large result)
        # with an explicit score/ref ordering that is independent of storage
        # and provider iteration order.
        hits = sorted(hits, key=lambda hit: (-hit.score, hit.knowledge_ref))[
            :MAX_SEMANTIC_CANDIDATES
        ]
        identity = validate_embedding_provider(self.index.provider)
        candidates = tuple(
            SemanticCandidate(
                hit.knowledge_ref,
                hit.score,
                identity.provider_id,
                identity.model_id,
                identity.embedding_dimension,
            )
            for hit in hits
        )
        return SemanticCandidateResult(
            candidates,
            synchronization,
            records_seen=len(eligible),
            records_truncated=truncated,
        )

    # Explicit aliases keep the internal API easy to discover without creating
    # a caller-facing Level 1 orchestration surface.
    candidates = generate
    search = generate


SemanticCandidateGenerator = SemanticCandidateService
