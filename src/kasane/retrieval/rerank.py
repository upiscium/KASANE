"""Small, authority-free boundary for Level 1 reranking."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .fake_embeddings import DeterministicEmbeddingProvider

MAX_RERANK_CANDIDATES = 32
# This is a provider-input byte limit, not a character limit.  Components are
# bounded before they are joined so a hostile record cannot force a full-size
# concatenation merely to discard most of it.
MAX_PROVIDER_DOCUMENT_BYTES = 2048
# Kept as a compatibility name for callers that already use the public limit.
MAX_PROVIDER_DOCUMENT_CHARS = MAX_PROVIDER_DOCUMENT_BYTES


@dataclass(frozen=True)
class RerankCandidate:
    """The only knowledge a reranker receives (never a KnowledgeRecord)."""

    knowledge_ref: str
    document: str
    lexical_score: float | None = None
    semantic_score: float | None = None


@dataclass(frozen=True)
class RerankedCandidate:
    knowledge_ref: str
    score: float


def union_candidates(
    lexical: Sequence[RerankCandidate], semantic: Sequence[RerankCandidate]
) -> tuple[RerankCandidate, ...]:
    """Union by public ref, merging source signals without admitting new refs."""
    merged: dict[str, RerankCandidate] = {}
    for candidate in (*lexical, *semantic):
        previous = merged.get(candidate.knowledge_ref)
        if previous is None:
            merged[candidate.knowledge_ref] = candidate
        else:
            merged[candidate.knowledge_ref] = RerankCandidate(
                candidate.knowledge_ref,
                previous.document,
                previous.lexical_score
                if previous.lexical_score is not None
                else candidate.lexical_score,
                previous.semantic_score
                if previous.semantic_score is not None
                else candidate.semantic_score,
            )
    return tuple(merged.values())


class RerankProvider(Protocol):
    provider_id: str
    model_id: str

    def rerank(
        self, query: str, candidates: Sequence[RerankCandidate]
    ) -> Sequence[RerankedCandidate]: ...


class DeterministicRerankProvider:
    """A reproducible local provider suitable for tests and offline operation."""

    provider_id = "test:deterministic-reranker"
    model_id = "test:hybrid-concepts-v1"

    def __init__(self) -> None:
        self._embeddings = DeterministicEmbeddingProvider()

    def rerank(
        self, query: str, candidates: Sequence[RerankCandidate]
    ) -> tuple[RerankedCandidate, ...]:
        query_vector = self._embeddings.embed_query(query)
        scored: list[RerankedCandidate] = []
        for candidate in candidates:
            semantic = candidate.semantic_score or 0.0
            # The deterministic embedding provider is the semantic signal; the
            # lexical signal is only a tie breaker.  This is intentionally not
            # an authority decision and cannot introduce a new reference.
            document_vector = self._embeddings.embed_documents([candidate.document])[0]
            cosine_like = sum(a * b for a, b in zip(query_vector, document_vector))
            query_lower = query.casefold()
            document_lower = candidate.document.casefold()
            real_systems_bonus = (
                1.0
                if "real systems" in query_lower
                and any(
                    term in document_lower
                    for term in ("live", "customer-facing", "operational traffic")
                )
                else 0.0
            )
            lexical = 1.0 / (1.0 + max(candidate.lexical_score or 0.0, 0.0))
            scored.append(
                RerankedCandidate(
                    candidate.knowledge_ref,
                    semantic + cosine_like + real_systems_bonus + lexical * 0.01,
                )
            )
        return tuple(sorted(scored, key=lambda item: (-item.score, item.knowledge_ref)))


def bounded_document(
    title: str, body: str, limit: int = MAX_PROVIDER_DOCUMENT_BYTES
) -> str:
    """Build bounded provider input without exposing arbitrary record fields.

    The total UTF-8 encoding is at most ``limit`` bytes.  Title gets first
    priority, followed by body, with one separator byte reserved only when
    both components are present.  Prefixes are collected by UTF-8 width
    without encoding or concatenating the unbounded inputs.
    """
    if limit < 0:
        raise ValueError("document byte limit must be non-negative")

    def prefix(text: str, budget: int) -> str:
        """Inspect at most one provider-sized prefix of ``text``.

        Whitespace trimming must happen after bounding the input.  In
        particular, ``rstrip`` on the original value would scan an attacker
        controlled trailing run even when the meaningful prefix is tiny.
        """
        used = 0
        stop = 0
        while stop < len(text):
            width = len(text[stop].encode("utf-8"))
            if used + width > budget:
                break
            used += width
            stop += 1
        return text[:stop].strip()

    title_part = prefix(title, limit)
    body_part = prefix(body, limit)
    title_present = bool(title_part)
    body_present = bool(body_part)
    separator = 1 if title_present and body_present and limit else 0
    remaining = limit - len(title_part.encode("utf-8"))
    if remaining <= separator:
        return title_part
    body_part = prefix(body, remaining - separator)
    if title_part and body_part:
        return f"{title_part}\n{body_part}"
    return title_part or body_part
