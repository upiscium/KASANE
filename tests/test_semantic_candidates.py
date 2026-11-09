from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

from test_semantic_index import FakeProvider, record

from kasane.retrieval import (
    SemanticCandidateService,
)
from kasane.retrieval.semantic_candidates import (
    MAX_SEMANTIC_CANDIDATES,
    MAX_SEMANTIC_RECORDS,
)
from kasane.retrieval.semantic_index import DerivedSemanticIndex
from kasane.vault.models import KnowledgeStatus


def test_candidates_are_traceable_and_gate_all_ineligible_records(tmp_path) -> None:
    provider = FakeProvider()
    service = SemanticCandidateService(
        DerivedSemanticIndex(
            tmp_path / "semantic.db",
            provider,
            canonical_database_path=tmp_path / "vault.db",
        )
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    eligible = record("vault://project/ok", "first")
    records = [
        eligible,
        replace(eligible, knowledge_ref="vault://other/no-scope"),
        replace(
            eligible,
            knowledge_ref="vault://project/deprecated",
            status=KnowledgeStatus.DEPRECATED,
        ),
        replace(
            eligible,
            knowledge_ref="vault://project/stale",
            stale_after="2025-01-01T00:00:00Z",
        ),
        replace(
            eligible,
            knowledge_ref="vault://project/malformed",
            stale_after="not-a-date",
        ),
        replace(
            eligible,
            knowledge_ref="vault://project/conditional",
            applies_when={"x": 1},
        ),
    ]
    result = service.generate(records, "query", ["project"], now)
    assert [candidate.knowledge_ref for candidate in result.candidates] == [
        eligible.knowledge_ref
    ]
    candidate = result.candidates[0]
    assert (candidate.semantic_score, candidate.provider_id, candidate.model_id) == (
        1.0,
        "test-provider",
        "test-model",
    )
    assert candidate.embedding_dimension == 2
    assert provider.document_calls == 1


def test_search_identity_mismatch_is_rebuilt_before_results_are_returned(
    tmp_path,
) -> None:
    provider = FakeProvider()
    index = DerivedSemanticIndex(
        tmp_path / "semantic.db",
        provider,
        canonical_database_path=tmp_path / "vault.db",
    )
    service = SemanticCandidateService(index)
    item = record("vault://project/item")
    service.generate([item], "query", ["project"])
    with sqlite3.connect(index.database_path) as connection:
        connection.execute(
            "UPDATE semantic_metadata SET value = 'wrong' WHERE key = 'model_id'"
        )
    result = service.generate([item], "query", ["project"])
    assert result.synchronization.rebuilt
    assert result.candidates[0].model_id == "test-model"


def test_large_eligible_input_is_bounded_before_embedding_provider_call(
    tmp_path,
) -> None:
    provider = FakeProvider()
    service = SemanticCandidateService(
        DerivedSemanticIndex(
            tmp_path / "semantic.db",
            provider,
            canonical_database_path=tmp_path / "vault.db",
        )
    )
    records = [record(f"vault://project/{index:03d}") for index in range(300)]
    result = service.generate(records, "query", ["project"])
    assert result.records_seen == 300
    assert result.records_truncated == 300 - MAX_SEMANTIC_RECORDS
    assert provider.document_calls == 1
    assert provider.last_documents_count == MAX_SEMANTIC_RECORDS


def test_large_search_result_is_ranked_and_capped_deterministically(tmp_path) -> None:
    provider = FakeProvider()
    service = SemanticCandidateService(
        DerivedSemanticIndex(
            tmp_path / "semantic.db",
            provider,
            canonical_database_path=tmp_path / "vault.db",
        )
    )
    records = [record(f"vault://project/{index:02d}", "first") for index in range(40)]

    result = service.generate(records, "query", ["project"])

    assert len(result.candidates) == MAX_SEMANTIC_CANDIDATES
    assert [item.knowledge_ref for item in result.candidates] == [
        f"vault://project/{index:02d}" for index in range(MAX_SEMANTIC_CANDIDATES)
    ]
