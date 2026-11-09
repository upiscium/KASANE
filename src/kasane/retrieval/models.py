"""Internal Level 0 retrieval models kept outside the wire schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RetrievalBudget:
    max_tokens: int
    max_bytes: int
    max_evidence_items: int


@dataclass(frozen=True)
class RetrievalTask:
    summary: str | None
    repository: str | None
    languages: tuple[str, ...]
    topics: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    scope: tuple[str, ...]
    mode: str
    budget: RetrievalBudget
    tokenizer_id: str | None
    task: RetrievalTask | None


@dataclass(frozen=True)
class RetrievalDiagnostics:
    candidate_count: int
    selected_count: int
    excluded_scope_count: int
    excluded_lifecycle_count: int
    excluded_applicability_count: int
    excluded_stale_count: int
    malformed_freshness_count: int
    index_rebuilt: bool
    index_watermark: str
    serialized_bytes: int
    elapsed_ms: float
    level: int = 0
    path: tuple[int, ...] = (0,)
    lexical_candidate_count: int = 0
    semantic_candidate_count: int = 0
    reranker_provider_id: str | None = None
    reranker_model_id: str | None = None
    fallback_reason: str | None = None
    semantic_records_seen: int = 0
    semantic_records_truncated: int = 0
    semantic_record_cap: int | None = None


@dataclass(frozen=True)
class RetrievalResult:
    capsule: dict[str, Any] | None
    error: dict[str, Any] | None
    diagnostics: RetrievalDiagnostics

    @property
    def artifact(self) -> dict[str, Any]:
        artifact = self.capsule if self.capsule is not None else self.error
        if artifact is None:  # pragma: no cover - constructor invariant
            raise RuntimeError("retrieval result has no artifact")
        return artifact


@dataclass(frozen=True)
class RankedKnowledge:
    knowledge_ref: str
    lexical_score: float
    exact_title: bool
    exact_tag: bool
    exact_topic: bool
    scope_specificity: int
