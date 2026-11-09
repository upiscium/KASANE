"""Deterministic Level 0 retrieval public API."""

from .budget import ExactTokenCounter, accounting_payload
from .eligibility import EligibilityCounts, EligibilityResult, RetrievalEligibility
from .embeddings import (
    MAX_SEMANTIC_DOCUMENT_BYTES,
    SEMANTIC_DOCUMENT_REPRESENTATION_VERSION,
    EmbeddingProvider,
    EmbeddingProviderIdentity,
    EmbeddingValidationError,
    embed_documents,
    embed_query,
    embedding_document,
    semantic_document,
    semantic_document_representation,
    validate_embedding_provider,
    validate_embedding_vector,
    validate_embedding_vectors,
    validate_provider_identity,
)
from .fake_embeddings import DeterministicEmbeddingProvider
from .models import RetrievalDiagnostics, RetrievalResult
from .rerank import (
    DeterministicRerankProvider,
    RerankCandidate,
    RerankedCandidate,
    RerankProvider,
    union_candidates,
)
from .semantic_candidates import (
    SemanticCandidate,
    SemanticCandidateGenerator,
    SemanticCandidateResult,
    SemanticCandidateService,
)
from .service import (
    HybridRetrievalService,
    Level0RetrievalService,
    Level1RetrievalService,
)

__all__ = [
    "MAX_SEMANTIC_DOCUMENT_BYTES",
    "SEMANTIC_DOCUMENT_REPRESENTATION_VERSION",
    "DeterministicEmbeddingProvider",
    "DeterministicRerankProvider",
    "EligibilityCounts",
    "EligibilityResult",
    "EmbeddingProvider",
    "EmbeddingProviderIdentity",
    "EmbeddingValidationError",
    "ExactTokenCounter",
    "HybridRetrievalService",
    "Level0RetrievalService",
    "Level1RetrievalService",
    "RerankCandidate",
    "RerankProvider",
    "RerankedCandidate",
    "RetrievalDiagnostics",
    "RetrievalEligibility",
    "RetrievalResult",
    "SemanticCandidate",
    "SemanticCandidateGenerator",
    "SemanticCandidateResult",
    "SemanticCandidateService",
    "accounting_payload",
    "embed_documents",
    "embed_query",
    "embedding_document",
    "semantic_document",
    "semantic_document_representation",
    "union_candidates",
    "validate_embedding_provider",
    "validate_embedding_vector",
    "validate_embedding_vectors",
    "validate_provider_identity",
]
