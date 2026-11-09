from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

from kasane.retrieval import (
    SEMANTIC_DOCUMENT_REPRESENTATION_VERSION,
    EmbeddingProvider,
    EmbeddingProviderIdentity,
    EmbeddingValidationError,
    embed_documents,
    embedding_document,
    validate_embedding_vector,
    validate_embedding_vectors,
    validate_provider_identity,
)
from kasane.vault.models import KnowledgeRecord, KnowledgeStatus


def record() -> KnowledgeRecord:
    return KnowledgeRecord(
        knowledge_ref="vault://project/demo/path",
        namespace="project/demo",
        knowledge_path="path",
        knowledge_type="contract",
        title="A title",
        body="The body.",
        status=KnowledgeStatus.CANONICAL,
        stability=None,
        tags=["zeta", "alpha"],
        scope={},
        applies_when={},
        counterconditions=[],
        sources=[],
        provenance={},
        generated={},
        verification={},
        verification_outcome=None,
        stale_after=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        revision=1,
    )


@pytest.mark.parametrize("dimension", [0, -1, True, "3"])
def test_provider_identity_requires_nonempty_names_and_positive_dimension(
    dimension: object,
) -> None:
    with pytest.raises(EmbeddingValidationError):
        validate_provider_identity("provider", "model", dimension)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingValidationError):
        validate_provider_identity(" ", "model", 3)
    with pytest.raises(EmbeddingValidationError):
        validate_provider_identity("provider", "", 3)


@pytest.mark.parametrize(
    "vector",
    [
        [1, 2],
        [1, float("nan"), 3],
        [1, float("inf"), 3],
        [True, 2, 3],
        ["1", 2, 3],
    ],
)
def test_vectors_require_exact_dimension_and_finite_values(vector: object) -> None:
    with pytest.raises(EmbeddingValidationError):
        validate_embedding_vector(vector, 3)  # type: ignore[arg-type]
    assert validate_embedding_vector([1, 2, 3], 3) == (1.0, 2.0, 3.0)


def test_batch_validation_preserves_order_and_shape() -> None:
    assert validate_embedding_vectors([[1, 2], [3, 4]], 2) == (
        (1.0, 2.0),
        (3.0, 4.0),
    )


def test_identity_dataclass_validates_at_construction() -> None:
    assert EmbeddingProviderIdentity("local", "test-model", 2).embedding_dimension == 2
    with pytest.raises(EmbeddingValidationError):
        EmbeddingProviderIdentity("local", "test-model", 0)


def test_embedding_document_is_versioned_canonical_json_and_preserves_tag_order() -> (
    None
):
    first = embedding_document(record())
    second = embedding_document(replace(record(), tags=["alpha", "zeta"]))
    assert first == (
        '{"body":"The body.","knowledge_path":"path",'
        '"namespace":"project/demo",'
        f'"representation_version":"{SEMANTIC_DOCUMENT_REPRESENTATION_VERSION}",'
        '"tags":["zeta","alpha"],"title":"A title"}'
    )
    assert first != second


def test_document_batch_validation_requires_one_vector_per_text() -> None:
    class Provider:
        provider_id = "local"
        model_id = "test-model"
        embedding_dimension = 2

        def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            return [[1, 2]] if texts else []

        def embed_query(self, text: str) -> list[float]:
            return [1, 2]

    provider: EmbeddingProvider = Provider()
    with pytest.raises(EmbeddingValidationError, match="batch"):
        embed_documents(provider, ["one", "two"])
