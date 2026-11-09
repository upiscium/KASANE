from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_level0_retrieval import NOW, draft, request, store

from kasane import VaultRepository
from kasane.retrieval import Level1RetrievalService
from kasane.retrieval.budget import BudgetAccountant
from kasane.retrieval.request import parse_retrieval_request
from kasane.retrieval.rerank import (
    MAX_PROVIDER_DOCUMENT_BYTES,
    MAX_RERANK_CANDIDATES,
    RerankCandidate,
    RerankedCandidate,
    bounded_document,
    union_candidates,
)
from kasane.retrieval.semantic_candidates import (
    SemanticCandidate,
)


@pytest.fixture
def repository(tmp_path):
    return VaultRepository(tmp_path / "vault.db")


def test_union_merges_signals_and_deduplicates_by_public_ref() -> None:
    merged = union_candidates(
        [
            RerankCandidate("a", "doc-a", lexical_score=2.0),
            RerankCandidate("a", "doc-a", lexical_score=3.0),
        ],
        [
            RerankCandidate("a", "doc-a", semantic_score=0.9),
            RerankCandidate("b", "doc-b", semantic_score=0.8),
        ],
    )
    assert merged == (
        RerankCandidate("a", "doc-a", lexical_score=2.0, semantic_score=0.9),
        RerankCandidate("b", "doc-b", semantic_score=0.8),
    )


def test_mixed_pressure_reserves_late_canonical_winner_and_semantic_opportunity(
    repository, tmp_path
) -> None:
    records = [
        store(
            repository,
            draft(
                f"vault://global/level1/pressure-{index:02d}",
                title="Lexical pressure",
                body="shared pressure",
            ),
        )
        for index in range(MAX_RERANK_CANDIDATES)
    ]
    winner = store(
        repository,
        draft(
            "vault://global/level1/canonical-winner",
            title="Canonical winner",
            body="late lexical hit",
        ),
    )
    semantic = [
        RerankCandidate(winner.knowledge_ref, "semantic"),
        *(
            RerankCandidate(f"vault://global/level1/semantic-{index}", "semantic")
            for index in range(MAX_RERANK_CANDIDATES // 4)
        ),
    ]
    # The Level 0 winner is semantic-only relative to the capped lexical
    # input. It must consume one slot, not appear twice under overlap pressure.
    candidates = Level1RetrievalService._mixed_candidates(
        [RerankCandidate(item.knowledge_ref, "lexical") for item in records],
        semantic,
        winner.knowledge_ref,
        {item.knowledge_ref: item for item in [*records, winner]},
    )
    assert len(candidates) == MAX_RERANK_CANDIDATES
    refs = [item.knowledge_ref for item in candidates]
    assert len(refs) == len(set(refs)) == MAX_RERANK_CANDIDATES
    assert refs[:8] == [
        winner.knowledge_ref,
        *(item.knowledge_ref for item in semantic[1:8]),
    ]
    assert refs[8:] == [item.knowledge_ref for item in records[:24]]
    assert all(
        len(item.document.encode("utf-8")) <= MAX_PROVIDER_DOCUMENT_BYTES
        for item in candidates
    )


def test_explicit_semantic_index_path_accepts_canonical_directory_sibling(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "vault.sqlite"
    Level1RetrievalService._validate_semantic_index_path(
        tmp_path / "semantic.sqlite", canonical
    )


def test_explicit_semantic_index_path_rejects_path_outside_canonical_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError, match="inside the canonical Vault database directory"
    ):
        Level1RetrievalService._validate_semantic_index_path(
            tmp_path.parent / "semantic.sqlite", tmp_path / "vault.sqlite"
        )


def test_bounded_document_is_the_only_reranker_document_input() -> None:
    assert bounded_document("title", "x" * 20, limit=10) == "title\nxxxx"


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 6, 7])
def test_bounded_document_enforces_utf8_byte_boundary(limit: int) -> None:
    document = bounded_document("é", "界" * 100, limit=limit)
    assert len(document.encode("utf-8")) <= limit


def test_bounded_document_does_not_allocate_unbounded_joined_input() -> None:
    document = bounded_document("title" * 1_000_000, "body" * 1_000_000)
    assert len(document.encode("utf-8")) <= MAX_PROVIDER_DOCUMENT_BYTES


def test_bounded_document_does_not_scan_huge_trailing_whitespace() -> None:
    document = bounded_document("title" + " " * 10_000_000, "body")
    assert document == "title\nbody"
    assert len(document.encode("utf-8")) <= MAX_PROVIDER_DOCUMENT_BYTES


class CountingSemantic:
    def __init__(self, candidates=()):
        self.calls = 0
        self.candidates = tuple(candidates)

    def generate(self, records, query, scope, now):
        self.calls += 1
        return SimpleNamespace(
            candidates=self.candidates,
            synchronization=SimpleNamespace(rebuilt=False, watermark="semantic-test"),
        )


class CountingReranker:
    provider_id = "test:counting"
    model_id = "test:counting-v1"

    def __init__(self, output=None, error=None):
        self.calls = 0
        self.last_candidates = ()
        self.output = output
        self.error = error

    def rerank(self, query, candidates):
        self.calls += 1
        self.last_candidates = tuple(candidates)
        if self.error:
            raise self.error
        return (
            self.output
            if self.output is not None
            else tuple(
                RerankedCandidate(item.knowledge_ref, float(index))
                for index, item in enumerate(candidates)
            )
        )


def _service(repository, tmp_path, *, semantic=None, reranker=None):
    from kasane import Level0RetrievalService

    level0 = Level0RetrievalService(
        repository, tmp_path / "lexical.db", clock=lambda: NOW
    )
    return Level1RetrievalService(level0, semantic_service=semantic, reranker=reranker)


def test_fast_mode_has_level0_parity_and_zero_provider_calls(
    repository, tmp_path
) -> None:
    record = store(
        repository,
        draft("vault://global/level1/fast", title="Fast", body="fast marker"),
    )
    semantic = CountingSemantic()
    reranker = CountingReranker()
    result = _service(
        repository, tmp_path, semantic=semantic, reranker=reranker
    ).retrieve(request("fast marker"))
    assert result.capsule["knowledge_refs"][0]["uri"] == record.knowledge_ref
    assert result.diagnostics.level == 0
    assert semantic.calls == reranker.calls == 0


def test_unconfigured_level1_providers_fall_back_to_level0(
    repository, tmp_path
) -> None:
    record = store(
        repository,
        draft(
            "vault://global/level1/unconfigured",
            title="Unconfigured",
            body="unconfigured marker",
        ),
    )

    result = _service(repository, tmp_path).retrieve(
        {**request("unconfigured marker"), "mode": "thorough"}
    )

    assert result.diagnostics.level == 0
    assert result.diagnostics.path == (0,)
    assert result.diagnostics.fallback_reason == "level1_provider_unavailable"
    assert result.capsule["knowledge_refs"][0]["uri"] == record.knowledge_ref


@pytest.mark.parametrize(
    "query",
    ["permission check at action moment", "TLS", "rankingsharedomega for real systems"],
)
def test_auto_escalates_deterministically_for_semantic_and_ranking_cases(
    query, repository, tmp_path
) -> None:
    record = store(
        repository,
        draft(
            "vault://global/level1/target",
            title="Transport Layer Security",
            body="rankingsharedomega real systems",
        ),
    )
    if query.startswith("rankingsharedomega"):
        for suffix in ("a", "b", "c"):
            store(
                repository,
                draft(
                    f"vault://global/level1/distractor-{suffix}",
                    title="Ranking distractor",
                    body="rankingsharedomega",
                ),
            )
    semantic = CountingSemantic(
        (SemanticCandidate(record.knowledge_ref, 1.0, "test", "model", 8),)
    )
    reranker = CountingReranker()
    result = _service(
        repository, tmp_path, semantic=semantic, reranker=reranker
    ).retrieve({**request(query, max_evidence_items=1), "mode": "auto"})
    assert result.diagnostics.level == 1
    assert result.diagnostics.path == (0, 1)
    assert semantic.calls == reranker.calls == 1


def test_auto_lexical_promotion_preserves_other_lexical_candidates(
    repository, tmp_path
) -> None:
    for index in range(4):
        store(
            repository,
            draft(
                f"vault://global/level1/lexical-{index}",
                title=f"Lexical {index}",
                body="shared preservation marker",
            ),
        )

    class ReverseReranker(CountingReranker):
        def rerank(self, query, candidates):
            self.calls += 1
            self.last_candidates = tuple(candidates)
            return tuple(
                RerankedCandidate(item.knowledge_ref, float(index))
                for index, item in enumerate(reversed(candidates))
            )

    reranker = ReverseReranker()
    result = _service(
        repository, tmp_path, semantic=CountingSemantic(), reranker=reranker
    ).retrieve(
        {
            **request("shared preservation marker", max_evidence_items=3),
            "mode": "auto",
        }
    )

    lexical_refs = [item.knowledge_ref for item in reranker.last_candidates]
    expected = [lexical_refs[0], *reversed(lexical_refs[1:])][:3]
    returned = [item["uri"] for item in result.capsule["knowledge_refs"]]
    assert result.diagnostics.level == 1
    assert returned == expected
    assert result.diagnostics.candidate_count == 4


def test_semantic_only_candidate_survives_lexical_provider_cap(
    repository, tmp_path
) -> None:
    for index in range(MAX_RERANK_CANDIDATES + 8):
        store(
            repository,
            draft(
                f"vault://global/level1/lexical-pressure-{index}",
                title=f"Pressure {index}",
                body="lexical pressure marker",
            ),
        )
    semantic_record = store(
        repository,
        draft(
            "vault://global/level1/semantic-recovery",
            title="Semantic recovery",
            body="unrelated document",
        ),
    )
    semantic = CountingSemantic(
        [SemanticCandidate(semantic_record.knowledge_ref, 1.0, "test", "model", 8)]
    )
    reranker = CountingReranker()

    result = _service(
        repository, tmp_path, semantic=semantic, reranker=reranker
    ).retrieve({**request("lexical pressure marker"), "mode": "thorough"})

    assert result.diagnostics.level == 1
    assert result.diagnostics.candidate_count == MAX_RERANK_CANDIDATES
    assert semantic_record.knowledge_ref in {
        item.knowledge_ref for item in reranker.last_candidates
    }


def test_thorough_always_uses_level1(repository, tmp_path) -> None:
    record = store(
        repository,
        draft(
            "vault://global/level1/thorough", title="Thorough", body="thorough marker"
        ),
    )
    semantic = CountingSemantic(
        (SemanticCandidate(record.knowledge_ref, 1.0, "test", "model", 8),)
    )
    reranker = CountingReranker()
    result = _service(
        repository, tmp_path, semantic=semantic, reranker=reranker
    ).retrieve({**request("thorough marker"), "mode": "thorough"})
    assert result.diagnostics.level == 1
    assert semantic.calls == reranker.calls == 1


@pytest.mark.parametrize("boundary", ["bytes", "exact_tokens"])
def test_level1_budget_preflight_rejects_one_below_minimum_without_providers(
    boundary, repository, tmp_path
) -> None:
    base = request("preflight", tokenizer="test:unicode-codepoint-v1")
    parsed = parse_retrieval_request({**base, "mode": "thorough"})
    counter = lambda payload: len(payload)
    accountant = BudgetAccountant(
        parsed,
        {"test:unicode-codepoint-v1": counter},
    )
    minimum = accountant.measure(accountant.minimum_failed_payload()).used
    value = {**base, "mode": "thorough"}
    if boundary == "bytes":
        value.pop("tokenizer")
        byte_parsed = parse_retrieval_request(value)
        minimum = (
            BudgetAccountant(byte_parsed, {})
            .measure(BudgetAccountant(byte_parsed, {}).minimum_failed_payload())
            .used
        )
        value["budget"] = {**value["budget"], "max_bytes": minimum - 1}
    else:
        value["budget"] = {**value["budget"], "max_tokens": minimum - 1}
    semantic = CountingSemantic()
    reranker = CountingReranker()
    service = _service(repository, tmp_path, semantic=semantic, reranker=reranker)
    service.level0._token_counters["test:unicode-codepoint-v1"] = counter

    result = service.retrieve(value)

    assert result.error["error"]["code"] == "budget_too_small_for_capsule"
    assert result.diagnostics.index_watermark == "not-synchronized"
    assert semantic.calls == reranker.calls == 0


@pytest.mark.parametrize("boundary", ["bytes", "exact_tokens"])
def test_level1_budget_preflight_accepts_actual_minimum_at_boundary(
    boundary, repository, tmp_path
) -> None:
    base = request("preflight", tokenizer="test:unicode-codepoint-v1")
    value = {**base, "mode": "thorough"}
    parsed = parse_retrieval_request(value)
    counter = lambda payload: len(payload)
    if boundary == "bytes":
        value.pop("tokenizer")
        parsed = parse_retrieval_request(value)
        accountant = BudgetAccountant(parsed, {})
        minimum = accountant.measure(
            accountant.minimum_failed_payload(level=1, path=(0, 1))
        ).used
        value["budget"] = {**value["budget"], "max_bytes": minimum}
    else:
        accountant = BudgetAccountant(parsed, {"test:unicode-codepoint-v1": counter})
        minimum = accountant.measure(
            accountant.minimum_failed_payload(level=1, path=(0, 1))
        ).used
        value["budget"] = {**value["budget"], "max_tokens": minimum}

    semantic = CountingSemantic()
    reranker = CountingReranker()
    service = _service(repository, tmp_path, semantic=semantic, reranker=reranker)
    service.level0._token_counters["test:unicode-codepoint-v1"] = counter
    result = service.retrieve(value)

    assert result.error is None
    assert result.capsule["retrieval"]["level"] == 1
    assert result.capsule["retrieval"]["path"] == [0, 1]
    assert result.diagnostics.level == 1


def test_auto_budget_fitting_level0_but_not_level1_stays_level0(
    repository, tmp_path
) -> None:
    base = request("budget boundary")
    parsed = parse_retrieval_request({**base, "mode": "auto"})
    accountant = BudgetAccountant(parsed, {})
    level0_minimum = accountant.measure(
        accountant.minimum_failed_payload(level=0, path=(0,))
    ).used
    level1_minimum = accountant.measure(
        accountant.minimum_failed_payload(level=1, path=(0, 1))
    ).used
    assert level0_minimum < level1_minimum
    value = {
        **base,
        "mode": "auto",
        "budget": {**base["budget"], "max_bytes": level0_minimum},
    }
    semantic = CountingSemantic()
    reranker = CountingReranker()

    result = _service(
        repository, tmp_path, semantic=semantic, reranker=reranker
    ).retrieve(value)

    assert result.diagnostics.level == 0
    assert result.diagnostics.path == (0,)
    assert semantic.calls == reranker.calls == 0


def test_provider_failure_falls_back_to_level0(repository, tmp_path) -> None:
    record = store(
        repository,
        draft(
            "vault://global/level1/fallback", title="Fallback", body="fallback marker"
        ),
    )
    result = _service(
        repository,
        tmp_path,
        semantic=CountingSemantic(),
        reranker=CountingReranker(error=RuntimeError("down")),
    ).retrieve({**request("fallback marker"), "mode": "thorough"})
    assert result.diagnostics.level == 0
    assert result.diagnostics.fallback_reason == "level1_provider_failure"
    assert result.capsule["knowledge_refs"][0]["uri"] == record.knowledge_ref


def test_malformed_reranker_output_falls_back_and_duplicates_are_safe(
    repository, tmp_path
) -> None:
    record = store(
        repository,
        draft("vault://global/level1/output", title="Output", body="output marker"),
    )
    candidate = SemanticCandidate(record.knowledge_ref, 1.0, "test", "model", 8)
    duplicate = CountingReranker(
        output=(
            RerankedCandidate(record.knowledge_ref, 1.0),
            RerankedCandidate(record.knowledge_ref, 0.5),
        )
    )
    result = _service(
        repository,
        tmp_path,
        semantic=CountingSemantic((candidate,)),
        reranker=duplicate,
    ).retrieve({**request("output marker"), "mode": "thorough"})
    assert result.diagnostics.level == 1
    malformed = _service(
        repository,
        tmp_path / "malformed",
        semantic=CountingSemantic((candidate,)),
        reranker=CountingReranker(output=(object(),)),
    ).retrieve({**request("output marker"), "mode": "thorough"})
    assert malformed.diagnostics.level == 0


def test_ineligible_semantic_output_falls_back(repository, tmp_path) -> None:
    store(
        repository,
        draft(
            "vault://global/level1/eligible",
            title="Eligible",
            body="eligibility marker",
        ),
    )
    semantic = CountingSemantic(
        (SemanticCandidate("vault://global/not-present", 1.0, "test", "model", 8),)
    )
    result = _service(
        repository, tmp_path, semantic=semantic, reranker=CountingReranker()
    ).retrieve({**request("eligibility marker"), "mode": "thorough"})
    assert result.diagnostics.level == 0


def test_large_semantic_output_is_rejected_before_iteration(
    repository, tmp_path
) -> None:
    records = [
        store(
            repository,
            draft(
                f"vault://global/level1/large-{index:02d}",
                title="Large",
                body="large marker",
            ),
        )
        for index in range(33)
    ]
    candidates = tuple(
        SemanticCandidate(item.knowledge_ref, 1.0, "test", "model", 8)
        for item in records
    )
    reranker = CountingReranker()
    result = _service(
        repository,
        tmp_path,
        semantic=CountingSemantic(candidates),
        reranker=reranker,
    ).retrieve({**request("large marker"), "mode": "thorough"})
    assert result.diagnostics.level == 0
    assert result.diagnostics.fallback_reason == "level1_provider_failure"


def test_large_reranker_output_is_rejected_before_iteration(
    repository, tmp_path
) -> None:
    record = store(
        repository,
        draft("vault://global/level1/large-output", title="Output", body="output"),
    )
    output = tuple(
        RerankedCandidate(record.knowledge_ref, 1.0)
        for _ in range(MAX_RERANK_CANDIDATES + 1)
    )
    result = _service(
        repository,
        tmp_path,
        semantic=CountingSemantic(
            (SemanticCandidate(record.knowledge_ref, 1.0, "test", "model", 8),)
        ),
        reranker=CountingReranker(output=output),
    ).retrieve({**request("output"), "mode": "thorough"})
    assert result.diagnostics.level == 0
    assert result.diagnostics.fallback_reason == "level1_provider_failure"


def test_hostile_scale_semantic_output_is_rejected_before_iteration(
    repository, tmp_path
) -> None:
    record = store(
        repository,
        draft(
            "vault://global/level1/semantic-large-output", title="Output", body="output"
        ),
    )
    candidate = SemanticCandidate(record.knowledge_ref, 1.0, "test", "model", 8)

    class HostileScaleOutput(list):
        def __iter__(self):
            raise AssertionError(
                "semantic output was iterated before its length was checked"
            )

    output = HostileScaleOutput([candidate] * 1_000_000)

    class HostileSemantic:
        def generate(self, records, query, scope, now):
            return SimpleNamespace(
                candidates=output,
                synchronization=SimpleNamespace(
                    rebuilt=False, watermark="semantic-test"
                ),
            )

    result = _service(
        repository,
        tmp_path,
        semantic=HostileSemantic(),
        reranker=CountingReranker(),
    ).retrieve({**request("output"), "mode": "thorough"})
    assert result.diagnostics.level == 0
    assert result.diagnostics.fallback_reason == "level1_provider_failure"


def test_list_subclass_reranker_output_is_rejected(repository, tmp_path) -> None:
    record = store(
        repository,
        draft("vault://global/level1/reranker-subclass", title="Output", body="output"),
    )

    class DeceptiveOutput(list):
        def __len__(self):
            return 0

    output = DeceptiveOutput([RerankedCandidate(record.knowledge_ref, 1.0)])
    result = _service(
        repository,
        tmp_path,
        semantic=CountingSemantic(),
        reranker=CountingReranker(output=output),
    ).retrieve({**request("output"), "mode": "thorough"})

    assert result.diagnostics.level == 0
    assert result.diagnostics.fallback_reason == "level1_provider_failure"
    assert result.capsule["knowledge_refs"][0]["uri"] == record.knowledge_ref


def test_many_large_lexical_hits_are_capped_before_reranker_document_preparation(
    repository, tmp_path
) -> None:
    for index in range(MAX_RERANK_CANDIDATES * 3):
        store(
            repository,
            draft(
                f"vault://global/level1/lexical-{index:03d}",
                title="shared lexical hit",
                body="shared lexical hit " + "x" * 10000,
            ),
        )
    reranker = CountingReranker()
    result = _service(
        repository,
        tmp_path,
        semantic=CountingSemantic(),
        reranker=reranker,
    ).retrieve({**request("shared lexical hit"), "mode": "thorough"})

    assert result.diagnostics.lexical_candidate_count == MAX_RERANK_CANDIDATES
    assert reranker.calls == 1
    assert len(reranker.last_candidates) == MAX_RERANK_CANDIDATES
    assert all(len(item.document) <= 2048 for item in reranker.last_candidates)


def test_index_failure_is_classified_separately(repository, tmp_path) -> None:
    record = store(
        repository, draft("vault://global/level1/index", title="Index", body="index")
    )

    class BrokenIndex:
        def generate(self, *args):
            raise sqlite3.DatabaseError("broken derived index")

    result = _service(
        repository, tmp_path, semantic=BrokenIndex(), reranker=CountingReranker()
    ).retrieve({**request("index"), "mode": "thorough"})
    assert result.diagnostics.fallback_reason == "level1_index_failure"
    assert result.capsule["knowledge_refs"][0]["uri"] == record.knowledge_ref


def test_internal_failure_is_not_labeled_as_provider_failure(
    repository, tmp_path
) -> None:
    class BrokenInternal:
        def generate(self, *args):
            raise KeyError("unexpected internal state")

    result = _service(
        repository, tmp_path, semantic=BrokenInternal(), reranker=CountingReranker()
    ).retrieve({**request("internal"), "mode": "thorough"})
    assert result.diagnostics.fallback_reason == "level1_internal_failure"
