from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from kasane.retrieval.embeddings import (
    MAX_SEMANTIC_DOCUMENT_BYTES,
    SEMANTIC_DOCUMENT_REPRESENTATION_VERSION,
    semantic_document,
)
from kasane.retrieval.semantic_candidates import SemanticCandidateService
from kasane.retrieval.semantic_index import DerivedSemanticIndex
from kasane.vault.models import KnowledgeRecord, KnowledgeStatus


class FakeProvider:
    provider_id = "test-provider"
    model_id = "test-model"
    embedding_dimension = 2

    def __init__(self) -> None:
        self.fail = False
        self.document_calls = 0
        self.last_documents_count = 0
        self.last_documents: tuple[str, ...] = ()

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        self.last_documents_count = len(texts)
        self.last_documents = tuple(texts)
        if self.fail:
            raise RuntimeError("provider unavailable")
        vectors: list[list[float]] = []
        for text in texts:
            if '"title":"best"' in text:
                vectors.append([1, 1])
            elif '"title":"first"' in text or '"title":"second"' in text:
                vectors.append([1, 0])
            else:
                vectors.append([0, 1])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("provider unavailable")
        return [1, 0]


def record(ref: str, title: str = "other", revision: int = 1) -> KnowledgeRecord:
    return KnowledgeRecord(
        knowledge_ref=ref,
        namespace="project/demo",
        knowledge_path=ref.rsplit("/", 1)[-1],
        knowledge_type="contract",
        title=title,
        body="The body.",
        status=KnowledgeStatus.CANONICAL,
        stability=None,
        tags=[],
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
        revision=revision,
    )


def records() -> list[KnowledgeRecord]:
    return [
        record("vault://a", "first"),
        record("vault://b", "best"),
        record("vault://c"),
    ]


def test_first_sync_noop_and_explicit_rebuild(tmp_path: Path) -> None:
    provider = FakeProvider()
    index = DerivedSemanticIndex(
        tmp_path / "semantic.sqlite",
        provider,
        canonical_database_path=tmp_path / "vault.sqlite",
    )
    first = index.synchronize(records())
    assert first.rebuilt and provider.document_calls == 1
    assert not index.synchronize(records()).rebuilt
    assert provider.document_calls == 1
    assert index.rebuild(records()).rebuilt
    assert provider.document_calls == 2


def test_large_bodies_are_bounded_before_embedding_and_keep_representation_version(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    source = record("vault://large", title="large")
    source = replace(source, body="x" * (MAX_SEMANTIC_DOCUMENT_BYTES * 4))
    index = DerivedSemanticIndex(
        tmp_path / "semantic.sqlite",
        provider,
        canonical_database_path=tmp_path / "vault.sqlite",
    )

    index.synchronize([source])

    document = semantic_document(source)
    assert len(document.encode("utf-8")) <= MAX_SEMANTIC_DOCUMENT_BYTES
    assert (
        f'"representation_version":"{SEMANTIC_DOCUMENT_REPRESENTATION_VERSION}"'
        in document
    )
    assert provider.last_documents == (document,)


def test_all_semantic_document_fields_are_bounded_in_actual_provider_payload(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    source = replace(
        record("vault://large", title="title"),
        title="é" * MAX_SEMANTIC_DOCUMENT_BYTES,
        tags=["标签" * MAX_SEMANTIC_DOCUMENT_BYTES] * 4,
        namespace="命名空间" * MAX_SEMANTIC_DOCUMENT_BYTES,
        knowledge_path="路径" * MAX_SEMANTIC_DOCUMENT_BYTES,
        body="正文" * MAX_SEMANTIC_DOCUMENT_BYTES,
    )
    index = DerivedSemanticIndex(
        tmp_path / "semantic.sqlite",
        provider,
        canonical_database_path=tmp_path / "vault.sqlite",
    )

    index.synchronize([source])

    payload = provider.last_documents[0]
    assert len(payload.encode("utf-8")) <= MAX_SEMANTIC_DOCUMENT_BYTES
    assert (
        f'"representation_version":"{SEMANTIC_DOCUMENT_REPRESENTATION_VERSION}"'
        in payload
    )
    assert provider.last_documents == (semantic_document(source),)


def test_corrupt_deleted_and_incompatible_databases_recover(tmp_path: Path) -> None:
    path = tmp_path / "semantic.sqlite"
    provider = FakeProvider()
    index = DerivedSemanticIndex(
        path, provider, canonical_database_path=tmp_path / "vault.sqlite"
    )
    index.synchronize(records())
    path.write_bytes(b"not sqlite")
    assert index.synchronize(records()).rebuilt
    path.unlink()
    assert index.synchronize(records()).rebuilt

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE semantic_metadata SET value = 'wrong-model' WHERE key = 'model_id'"
        )
    assert index.synchronize(records()).rebuilt
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE semantic_metadata SET value = 'old-representation' "
            "WHERE key = 'representation_version'"
        )
    assert index.synchronize(records()).rebuilt


def test_provider_model_dimension_and_representation_mismatch_rebuild(
    tmp_path: Path,
) -> None:
    path = tmp_path / "semantic.sqlite"
    index = DerivedSemanticIndex(
        path, FakeProvider(), canonical_database_path=tmp_path / "vault.sqlite"
    )
    index.synchronize(records())
    for key in (
        "provider_id",
        "model_id",
        "embedding_dimension",
        "representation_version",
    ):
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE semantic_metadata SET value = ? WHERE key = ?",
                ("mismatch", key),
            )
        assert index.synchronize(records()).rebuilt


def test_canonical_revision_status_and_document_changes_rebuild(tmp_path: Path) -> None:
    index = DerivedSemanticIndex(
        tmp_path / "semantic.sqlite",
        FakeProvider(),
        canonical_database_path=tmp_path / "vault.sqlite",
    )
    source = records()
    index.synchronize(source)
    changes = [
        replace(source[0], revision=2),
        replace(source[0], status=KnowledgeStatus.DEPRECATED),
        replace(source[0], body="Changed body."),
    ]
    for changed in changes:
        assert index.synchronize([changed, *source[1:]]).rebuilt
        source = [changed, *source[1:]]


def test_vector_integrity_corruption_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "semantic.sqlite"
    provider = FakeProvider()
    index = DerivedSemanticIndex(
        path, provider, canonical_database_path=tmp_path / "vault.sqlite"
    )
    index.synchronize(records())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE semantic_vectors SET vector = ? WHERE knowledge_ref = ?",
            (b"bad", "vault://a"),
        )
    assert index.synchronize(records()).rebuilt
    assert len(index.search("query", ["vault://a"])) == 1


def test_provider_failure_does_not_replace_published_index(tmp_path: Path) -> None:
    path = tmp_path / "semantic.sqlite"
    provider = FakeProvider()
    index = DerivedSemanticIndex(
        path, provider, canonical_database_path=tmp_path / "vault.sqlite"
    )
    index.synchronize(records())
    provider.fail = True
    with pytest.raises(RuntimeError, match="unavailable"):
        index.synchronize([replace(records()[0], body="new")] + records()[1:])
    provider.fail = False
    assert [hit.knowledge_ref for hit in index.search("query", ["vault://a"])] == [
        "vault://a"
    ]


def test_whole_file_corruption_between_sync_and_search_recovers_safely(
    tmp_path: Path,
) -> None:
    path = tmp_path / "semantic.sqlite"
    canonical = tmp_path / "vault.sqlite"
    canonical.write_bytes(b"canonical state")
    provider = FakeProvider()
    index = DerivedSemanticIndex(path, provider, canonical_database_path=canonical)
    service = SemanticCandidateService(index)
    index.synchronize(records())
    canonical_before = canonical.read_bytes()
    original_search = index.search
    corrupted = False

    def corrupt_then_search(query: str, allowed_refs: list[str]):
        nonlocal corrupted
        if not corrupted:
            path.write_bytes(b"whole-file corruption")
            corrupted = True
        return original_search(query, allowed_refs)

    index.search = corrupt_then_search  # type: ignore[assignment]
    result = service.generate(records(), "query", ["a", "b", "c"])

    assert result.candidates
    assert result.synchronization.rebuilt
    assert canonical.read_bytes() == canonical_before


def test_hard_link_alias_is_rejected_without_canonical_mutation(tmp_path: Path) -> None:
    canonical = tmp_path / "vault.sqlite"
    derived = tmp_path / "semantic.sqlite"
    canonical.write_bytes(b"canonical state")
    canonical_before = canonical.read_bytes()
    derived.hardlink_to(canonical)

    with pytest.raises(ValueError, match="alias the canonical"):
        DerivedSemanticIndex(
            derived,
            FakeProvider(),
            canonical_database_path=canonical,
        )
    assert canonical.read_bytes() == canonical_before


def test_symlink_swap_after_construction_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "vault.sqlite"
    derived = tmp_path / "semantic.sqlite"
    canonical.write_bytes(b"canonical state")
    canonical_before = canonical.read_bytes()
    index = DerivedSemanticIndex(
        derived,
        FakeProvider(),
        canonical_database_path=canonical,
    )
    derived.symlink_to(canonical)

    with pytest.raises(ValueError, match="canonical Vault database"):
        index.synchronize(records())
    assert canonical.read_bytes() == canonical_before


def test_allowed_refs_filter_scores_descending_and_ties_by_ref(tmp_path: Path) -> None:
    index = DerivedSemanticIndex(
        tmp_path / "semantic.sqlite",
        FakeProvider(),
        canonical_database_path=tmp_path / "vault.sqlite",
    )
    index.synchronize(records())
    hits = index.search("query", ["vault://c", "vault://b", "vault://a"])
    assert [(hit.knowledge_ref, hit.score) for hit in hits] == [
        ("vault://a", 1.0),
        ("vault://b", 1.0),
        ("vault://c", 0.0),
    ]
    assert index.search("query", ["vault://missing"]) == []


def test_semantic_database_is_separate_from_canonical_vault_database(
    tmp_path: Path,
) -> None:
    semantic_path = tmp_path / "derived" / "semantic.sqlite"
    canonical_path = tmp_path / "vault.sqlite"
    index = DerivedSemanticIndex(
        semantic_path, FakeProvider(), canonical_database_path=canonical_path
    )
    index.synchronize(records())
    assert semantic_path.exists()
    assert not canonical_path.exists()
    assert semantic_path != canonical_path


@pytest.mark.parametrize("derived_name", ["vault.sqlite", "alias/../vault.sqlite"])
def test_canonical_and_derived_resolved_paths_are_rejected_without_mutation(
    tmp_path: Path, derived_name: str
) -> None:
    canonical_path = tmp_path / "vault.sqlite"
    derived_path = tmp_path / derived_name
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    with pytest.raises(ValueError, match="canonical Vault database"):
        DerivedSemanticIndex(
            derived_path,
            FakeProvider(),
            canonical_database_path=canonical_path,
        )
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
