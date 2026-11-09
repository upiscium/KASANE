"""Offline deterministic embedding provider used by tests and evaluations."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .embeddings import EmbeddingVector


class DeterministicEmbeddingProvider:
    """Small, explainable vector space for reproducible semantic fixtures.

    The aliases are intentionally limited to concepts represented in the golden
    corpus.  This is a fake provider, not a production language model.
    """

    provider_id = "test:deterministic-semantic"
    model_id = "test:golden-concepts-v1"
    embedding_dimension = 8

    _concepts = (
        ("authority", ("authorization", "permission", "authority")),
        ("effect_time", ("effect-time", "action moment", "side effects")),
        (
            "transport_security",
            ("transport layer security", "tls", "encrypted transport"),
        ),
        ("transactions", ("transaction", "atomic write")),
        ("cache", ("cache invalidation", "cached entries")),
        ("ranking", ("ranking", "rankingsharedomega")),
        ("request", ("request", "trace_id")),
        ("scope", ("scope",)),
    )

    @staticmethod
    def _normalise(text: str) -> str:
        return re.sub(r"\s+", " ", text.casefold().replace("_", " ")).strip()

    def _vector(self, text: str) -> tuple[float, ...]:
        normalised = self._normalise(text)
        return tuple(
            1.0 if any(alias in normalised for alias in aliases) else 0.0
            for _, aliases in self._concepts
        )

    def embed_documents(self, texts: Sequence[str]) -> Sequence[EmbeddingVector]:
        return tuple(self._vector(text) for text in texts)

    def embed_query(self, text: str) -> EmbeddingVector:
        return self._vector(text)
