from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from kasane import (
    EventType,
    KnowledgeDraft,
    KnowledgeRecord,
    KnowledgeStatus,
    Level0RetrievalService,
    VaultRepository,
    VerificationOutcome,
    accounting_payload,
)
from kasane.retrieval import RetrievalEligibility
from kasane.retrieval.request import parse_retrieval_request

ROOT = Path(__file__).parents[1]
CAPSULE_VALIDATOR = Draft202012Validator(
    json.loads((ROOT / "schemas/context-capsule.schema.json").read_text())
)
ERROR_VALIDATOR = Draft202012Validator(
    json.loads((ROOT / "schemas/retrieval-error.schema.json").read_text())
)
REQUEST_VALIDATOR = Draft202012Validator(
    json.loads((ROOT / "schemas/retrieval-request.schema.json").read_text())
)
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path: Path) -> VaultRepository:
    return VaultRepository(tmp_path / "vault.db")


@pytest.fixture
def service(repository: VaultRepository, tmp_path: Path) -> Level0RetrievalService:
    ticks = iter([1.0, 1.004] * 100)
    return Level0RetrievalService(
        repository,
        tmp_path / "derived-index.db",
        clock=lambda: NOW,
        monotonic=lambda: next(ticks),
    )


def request(
    query: str,
    *,
    scope: list[str] | None = None,
    max_tokens: int = 10_000,
    max_bytes: int = 20_000,
    max_evidence_items: int = 10,
    tokenizer: str | None = None,
    topics: list[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "0.1",
        "query": query,
        "mode": "fast",
        "budget": {
            "max_tokens": max_tokens,
            "max_bytes": max_bytes,
            "max_evidence_items": max_evidence_items,
        },
    }
    if scope is not None:
        value["scope"] = scope
    if tokenizer is not None:
        value["tokenizer"] = {"id": tokenizer}
    if topics is not None:
        value["task"] = {"topics": topics}
    REQUEST_VALIDATOR.validate(value)
    return value


def draft(
    knowledge_ref: str,
    *,
    title: str,
    body: str,
    tags: list[str] | None = None,
    stale_after: str | None = None,
    sources: Any = None,
    provenance: Any = None,
    applies_when: Any = None,
    counterconditions: Any = None,
) -> KnowledgeDraft:
    return KnowledgeDraft(
        knowledge_ref=knowledge_ref,
        knowledge_type="guidance",
        title=title,
        body=body,
        tags=tags or [],
        stale_after=stale_after,
        sources=(
            [{"kind": "repository", "uri": f"repo://fixtures/{title}"}]
            if sources is None
            else sources
        ),
        provenance={} if provenance is None else provenance,
        applies_when={} if applies_when is None else applies_when,
        counterconditions=[] if counterconditions is None else counterconditions,
    )


def store(
    repository: VaultRepository,
    value: KnowledgeDraft,
    status: KnowledgeStatus = KnowledgeStatus.CANONICAL,
) -> KnowledgeRecord:
    record = repository.create_candidate(value, actor="writer")
    if status is KnowledgeStatus.CANDIDATE:
        return record
    record = repository.record_verification(
        value.knowledge_ref,
        expected_revision=record.revision,
        actor="reviewer",
        outcome=VerificationOutcome.PASSED,
        verification={"method": "fixture"},
    )
    record = repository.transition_lifecycle(
        value.knowledge_ref,
        KnowledgeStatus.VERIFIED,
        expected_revision=record.revision,
        actor="reviewer",
    )
    if status is KnowledgeStatus.VERIFIED:
        return record
    record = repository.transition_lifecycle(
        value.knowledge_ref,
        KnowledgeStatus.CANONICAL,
        expected_revision=record.revision,
        actor="promoter",
    )
    if status is KnowledgeStatus.CANONICAL:
        return record
    record = repository.transition_lifecycle(
        value.knowledge_ref,
        KnowledgeStatus.DEPRECATED,
        expected_revision=record.revision,
        actor="maintainer",
    )
    if status is KnowledgeStatus.DEPRECATED:
        return record
    return repository.transition_lifecycle(
        value.knowledge_ref,
        KnowledgeStatus.ARCHIVED,
        expected_revision=record.revision,
        actor="maintainer",
    )


def assert_capsule(value: dict[str, Any], request_value: dict[str, Any]) -> None:
    CAPSULE_VALIDATOR.validate(value)
    projection = accounting_payload(value)
    budget = value["budget"]
    assert budget["requested_tokens"] == request_value["budget"]["max_tokens"]
    assert budget["requested_bytes"] == request_value["budget"]["max_bytes"]
    assert budget["serialized_bytes"] == len(projection)
    assert budget["used"] <= budget["hard_limit"]
    assert budget["evidence_items"] == len(value["evidence"])
    assert len(value["evidence"]) <= request_value["budget"]["max_evidence_items"]
    knowledge_ids = {item["id"] for item in value["knowledge_refs"]}
    assert all(item["knowledge_ref"] in knowledge_ids for item in value["evidence"])


def selected_uris(capsule: dict[str, Any]) -> list[str]:
    return [item["uri"] for item in capsule["knowledge_refs"]]


def test_exact_title_query_ranks_exact_title_first(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    exact = store(
        repository,
        draft(
            "vault://global/retrieval/exact-title",
            title="Deterministic ranking",
            body="Ranking behavior.",
        ),
    )
    store(
        repository,
        draft(
            "vault://global/retrieval/body-title",
            title="Other guidance",
            body="Deterministic ranking appears in the body.",
        ),
    )
    request_value = request("Deterministic ranking")

    result = service.retrieve(request_value)

    assert result.error is None
    assert result.capsule is not None
    assert selected_uris(result.capsule)[0] == exact.knowledge_ref
    assert_capsule(result.capsule, request_value)


def test_exact_tag_query_and_body_lexical_query(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    tagged = store(
        repository,
        draft(
            "vault://global/retrieval/tagged",
            title="Tagged item",
            body="General guidance.",
            tags=["effect-time"],
        ),
    )
    body = store(
        repository,
        draft(
            "vault://global/retrieval/body",
            title="Body item",
            body="Revalidate authority immediately before the native effect.",
        ),
    )

    tag_result = service.retrieve(request("effect-time"))
    body_result = service.retrieve(request("revalidate authority"))

    assert tag_result.capsule is not None
    assert selected_uris(tag_result.capsule)[0] == tagged.knowledge_ref
    assert body_result.capsule is not None
    assert selected_uris(body_result.capsule) == [body.knowledge_ref]


def _trust_fixture(
    repository: VaultRepository,
) -> dict[KnowledgeStatus, KnowledgeRecord]:
    statuses = [
        KnowledgeStatus.CANDIDATE,
        KnowledgeStatus.VERIFIED,
        KnowledgeStatus.CANONICAL,
        KnowledgeStatus.DEPRECATED,
        KnowledgeStatus.ARCHIVED,
    ]
    return {
        status: store(
            repository,
            draft(
                f"vault://global/trust/{status.value}",
                title=f"Trust {status.value}",
                body="shared eligibility marker",
            ),
            status,
        )
        for status in statuses
    }


def test_default_trust_floor_is_t1_with_labels(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    records = _trust_fixture(repository)

    result = service.retrieve(request("eligibility marker"))

    assert result.capsule is not None
    # Equal relevance: trust breaks the tie, canonical first.
    assert selected_uris(result.capsule) == [
        records[KnowledgeStatus.CANONICAL].knowledge_ref,
        records[KnowledgeStatus.VERIFIED].knowledge_ref,
    ]
    labels = [item["trust"] for item in result.capsule["knowledge_refs"]]
    assert labels == [{"tier": "T3"}, {"tier": "T1"}]
    assert "[knowledge-2, T1]" in result.capsule["context"]
    assert result.diagnostics.excluded_lifecycle_count == 3


@pytest.mark.parametrize(
    ("min_trust", "expected"),
    [
        ("T3", [KnowledgeStatus.CANONICAL]),
        ("T2", [KnowledgeStatus.CANONICAL]),
        (
            "T0",
            [
                KnowledgeStatus.CANONICAL,
                KnowledgeStatus.VERIFIED,
                KnowledgeStatus.CANDIDATE,
            ],
        ),
    ],
)
def test_min_trust_selects_tiers_and_never_retired(
    repository: VaultRepository,
    service: Level0RetrievalService,
    min_trust: str,
    expected: list[KnowledgeStatus],
) -> None:
    records = _trust_fixture(repository)

    result = service.retrieve({**request("eligibility marker"), "min_trust": min_trust})

    assert result.capsule is not None
    assert selected_uris(result.capsule) == [
        records[status].knowledge_ref for status in expected
    ]


def test_t2_requires_independent_successes(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    from kasane.trust import AgentContext, TrustService

    trust = TrustService(repository)
    ref = "vault://global/trust/earned"
    trust.propose(
        {
            "schema_version": "0.1",
            "knowledge_ref": ref,
            "knowledge_type": "procedure",
            "title": "Earned trust",
            "body": "tiered eligibility marker for approval",
            "sources": [{"source_type": "other", "handle": "test"}],
        },
        AgentContext("author", "s0", "m1"),
    )
    signal = {"schema_version": "0.1", "knowledge_ref": ref}
    trust.signal(
        {**signal, "kind": "verification", "verification": "passed", "note": "ok"},
        AgentContext("verifier", "s1", "m1"),
    )
    t2_request = {**request("tiered eligibility marker"), "min_trust": "T2"}
    assert (
        selected_uris(service.retrieve(t2_request).capsule or {"knowledge_refs": []})
        == []
    )

    for session in ("u1", "u2"):
        trust.signal(
            {**signal, "kind": "outcome", "outcome": "success", "note": "worked"},
            AgentContext("user", session, "m1"),
        )
    result = service.retrieve(t2_request)
    assert result.capsule is not None
    assert selected_uris(result.capsule) == [ref]
    assert result.capsule["knowledge_refs"][0]["trust"]["tier"] == "T2"


def test_canonical_pending_revalidation_is_served_and_labeled(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    record = store(
        repository,
        draft(
            "vault://global/trust/wobbly", title="Wobbly", body="revalidation marker"
        ),
    )
    repository.record_signal_event(
        record.knowledge_ref,
        EventType.REVALIDATION_REQUESTED,
        expected_revision=record.revision,
        actor="policy",
    )
    result = service.retrieve(request("revalidation marker"))
    assert result.capsule is not None
    assert result.capsule["knowledge_refs"][0]["trust"] == {
        "tier": "T3",
        "revalidation": True,
    }


def test_invalid_min_trust_is_rejected(service: Level0RetrievalService) -> None:
    with pytest.raises(ValueError, match="min_trust"):
        service.retrieve({**request("anything"), "min_trust": "T9"})


def test_extracted_eligibility_policy_matches_level0_filter(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    records = [
        store(
            repository,
            draft(
                "vault://global/policy/accepted",
                title="Accepted",
                body="policy equivalence marker",
            ),
        ),
        store(
            repository,
            draft(
                "vault://global/policy/malformed",
                title="Malformed",
                body="policy equivalence marker",
                stale_after="invalid",
            ),
        ),
        store(
            repository,
            draft(
                "vault://global/policy/conditional",
                title="Conditional",
                body="policy equivalence marker",
                applies_when={"environment": "prod"},
            ),
        ),
    ]
    parsed = service.retrieve(request("policy equivalence marker"))
    eligibility = RetrievalEligibility()
    direct_records, direct_counts = eligibility.filter(records, ("global",), NOW)
    filtered_records, filtered_counts = service._eligible_records(
        records, parse_retrieval_request(request("policy equivalence marker"))
    )

    assert parsed.diagnostics.excluded_stale_count == direct_counts.excluded_stale
    assert [item.knowledge_ref for item in direct_records] == [
        item.knowledge_ref for item in filtered_records
    ]
    assert direct_counts == type(direct_counts)(3, 0, 0, 1, 1, 1)
    assert filtered_counts == (3, 0, 0, 1, 1, 1)
    assert eligibility.evaluate(records[1], ("global",), NOW).exclusion_reason == (
        "malformed_freshness"
    )


def test_passed_verification_without_canonical_status_is_excluded(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    value = draft(
        "vault://global/trust/passed-candidate",
        title="Passed candidate",
        body="passed-only marker",
    )
    candidate = repository.create_candidate(value, actor="writer")
    repository.record_verification(
        value.knowledge_ref,
        expected_revision=candidate.revision,
        actor="reviewer",
        outcome=VerificationOutcome.PASSED,
        verification={"checks": ["source"]},
    )

    result = service.retrieve(request("passed-only marker"))

    assert result.capsule is not None
    assert result.capsule["status"] == "failed"
    assert result.diagnostics.excluded_lifecycle_count == 1


def test_freshness_null_future_stale_and_malformed_are_deterministic(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    null_record = store(
        repository,
        draft(
            "vault://global/freshness/no-expiry",
            title="No expiry",
            body="freshness-marker",
        ),
    )
    future_record = store(
        repository,
        draft(
            "vault://global/freshness/future",
            title="Future expiry",
            body="freshness-marker",
            stale_after="2026-08-22T12:00:01Z",
        ),
    )
    store(
        repository,
        draft(
            "vault://global/freshness/stale",
            title="Stale",
            body="freshness-marker",
            stale_after="2026-08-22T12:00:00Z",
        ),
    )
    store(
        repository,
        draft(
            "vault://global/freshness/malformed",
            title="Malformed",
            body="freshness-marker",
            stale_after="not-a-timestamp",
        ),
    )

    result = service.retrieve(request("freshness-marker"))

    assert result.capsule is not None
    assert set(selected_uris(result.capsule)) == {
        null_record.knowledge_ref,
        future_record.knowledge_ref,
    }
    assert result.diagnostics.excluded_stale_count == 2
    assert result.diagnostics.malformed_freshness_count == 1


def test_nested_scope_matching_uses_segment_boundaries(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    nested = store(
        repository,
        draft(
            "vault://global/agent-development/safe-checks",
            title="Nested scope",
            body="scope-marker",
        ),
    )
    store(
        repository,
        draft(
            "vault://global/agent-developer/lookalike",
            title="Lookalike scope",
            body="scope-marker",
        ),
    )
    store(
        repository,
        draft(
            "vault://project/terreate/safe-checks",
            title="Project scope",
            body="scope-marker",
        ),
    )

    result = service.retrieve(
        request("scope-marker", scope=["global/agent-development"])
    )

    assert result.capsule is not None
    assert selected_uris(result.capsule) == [nested.knowledge_ref]
    assert result.diagnostics.excluded_scope_count == 2


def test_omitted_scope_defaults_to_global(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    global_record = store(
        repository,
        draft(
            "vault://global/default-scope/item",
            title="Global item",
            body="default-scope-marker",
        ),
    )
    store(
        repository,
        draft(
            "vault://project/terreate/default-scope",
            title="Project item",
            body="default-scope-marker",
        ),
    )

    result = service.retrieve(request("default-scope-marker"))

    assert result.capsule is not None
    assert selected_uris(result.capsule) == [global_record.knowledge_ref]


@pytest.mark.parametrize(
    "query_value",
    [
        "safe words",
        '"safe words"',
        "safe!!! words???",
        "AND OR NOT NEAR",
        "  safe\n\t words   ",
    ],
)
def test_query_normalization_is_safe_for_punctuation_quotes_and_operators(
    repository: VaultRepository,
    service: Level0RetrievalService,
    query_value: str,
) -> None:
    store(
        repository,
        draft(
            "vault://global/query/safety",
            title="Safe words",
            body="safe words and or not near are ordinary searchable terms",
        ),
    )
    request_value = request(query_value)

    result = service.retrieve(request_value)

    assert result.capsule is not None
    assert_capsule(result.capsule, request_value)


def test_deterministic_tie_break_and_rebuild_parity(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    for suffix in ("b", "a"):
        store(
            repository,
            draft(
                f"vault://global/tie/{suffix}",
                title="Equal rank",
                body="identical tie marker",
            ),
        )
    request_value = request("tie marker")

    before = service.retrieve(request_value)
    service.rebuild_index()
    after = service.retrieve(request_value)

    assert before.capsule is not None and after.capsule is not None
    assert selected_uris(before.capsule) == [
        "vault://global/tie/a",
        "vault://global/tie/b",
    ]
    assert before.capsule == after.capsule


def test_exact_task_topic_contributes_to_deterministic_ranking(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    store(
        repository,
        draft(
            "vault://global/topics/a-other",
            title="Equal topic item",
            body="topic-ranking-marker",
            tags=["other"],
        ),
    )
    target = store(
        repository,
        draft(
            "vault://global/topics/z-target",
            title="Equal topic item",
            body="topic-ranking-marker",
            tags=["security"],
        ),
    )

    result = service.retrieve(request("topic-ranking-marker", topics=["security"]))

    assert result.capsule is not None
    assert selected_uris(result.capsule)[0] == target.knowledge_ref


def test_evidence_is_bounded_traceable_and_not_a_raw_body_dump(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    record = store(
        repository,
        draft(
            "vault://global/evidence/traceable",
            title="Traceable evidence",
            body=("prefix " * 100) + "trace token" + (" suffix" * 100),
        ),
    )
    request_value = request("trace token")

    result = service.retrieve(request_value)

    assert result.capsule is not None
    evidence = result.capsule["evidence"][0]
    reference = result.capsule["knowledge_refs"][0]
    assert len(evidence["excerpt"]) <= 320
    assert evidence["excerpt"] != record.body
    assert reference["uri"] == record.knowledge_ref
    assert reference["revision"] == str(record.revision)
    assert evidence["knowledge_ref"] == reference["id"]
    assert_capsule(result.capsule, request_value)


def test_evidence_item_hard_cap_is_enforced(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    for suffix in ("a", "b", "c"):
        store(
            repository,
            draft(
                f"vault://global/cap/{suffix}",
                title=f"Cap {suffix}",
                body="cap-marker",
            ),
        )
    request_value = request("cap-marker", max_evidence_items=1)

    result = service.retrieve(request_value)

    assert result.capsule is not None
    assert result.capsule["status"] == "degraded"
    assert len(result.capsule["evidence"]) == 1
    assert result.diagnostics.selected_count == 1
    assert_capsule(result.capsule, request_value)


def test_zero_evidence_cap_reports_cap_exhaustion_not_no_match(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    store(
        repository,
        draft(
            "vault://global/cap/zero",
            title="Zero evidence cap",
            body="zero-cap-marker",
        ),
    )
    request_value = request("zero-cap-marker", max_evidence_items=0)

    result = service.retrieve(request_value)

    assert result.capsule is not None
    assert result.capsule["status"] == "failed"
    assert result.capsule["retrieval"]["terminal_reason"] == "budget_limited"
    question = result.capsule["unresolved"][0]["question"]
    assert "max_evidence_items is zero" in question
    assert "no eligible" not in question
    assert result.diagnostics.selected_count == 0
    assert_capsule(result.capsule, request_value)


def test_hard_byte_budget_accounts_for_large_provenance(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    store(
        repository,
        draft(
            "vault://global/budget/provenance",
            title="Large provenance",
            body="provenance-marker",
            sources=[],
            provenance={"source_type": "repository", "handle": "p" * 1800},
        ),
    )
    large_request = request("provenance-marker", max_bytes=10_000)
    large = service.retrieve(large_request)
    assert large.capsule is not None
    assert large.capsule["evidence"][0]["provenance"]["handle"] == "p" * 1800
    assert large.capsule["budget"]["used"] == len(accounting_payload(large.capsule))

    bounded_request = request("provenance-marker", max_bytes=900)
    bounded = service.retrieve(bounded_request)

    assert bounded.capsule is not None
    assert bounded.capsule["status"] == "failed"
    assert_capsule(bounded.capsule, bounded_request)


def test_oversize_opaque_provenance_falls_back_without_truncation(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    oversized = "opaque://" + ("x" * 3000)
    record = store(
        repository,
        draft(
            "vault://global/provenance/oversized",
            title="Oversized provenance",
            body="oversized-provenance-marker",
            sources=[],
            provenance={"source_type": "repository", "handle": oversized},
        ),
    )
    request_value = request("oversized-provenance-marker")

    result = service.retrieve(request_value)

    assert result.capsule is not None
    returned = result.capsule["evidence"][0]["provenance"]
    assert returned == {"source_type": "other", "handle": record.knowledge_ref}
    assert returned["handle"] != oversized[:2048]
    assert_capsule(result.capsule, request_value)


def test_hard_byte_overflow_drops_lower_ranked_evidence_with_marker(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    for suffix in ("a", "b"):
        store(
            repository,
            draft(
                f"vault://global/byte-overflow/{suffix}",
                title=f"Byte overflow {suffix}",
                body="byte-overflow-marker",
            ),
        )
    one_item = service.retrieve(request("byte-overflow-marker", max_evidence_items=1))
    assert one_item.capsule is not None
    one_item_size = one_item.capsule["budget"]["used"]
    bounded_request = request(
        "byte-overflow-marker", max_bytes=one_item_size, max_evidence_items=2
    )

    bounded = service.retrieve(bounded_request)

    assert bounded.capsule is not None
    assert bounded.capsule["status"] == "degraded"
    assert len(bounded.capsule["evidence"]) == 1
    assert bounded.capsule["retrieval"]["terminal_reason"] == "budget_limited"
    assert bounded.capsule["unresolved"][0]["kind"] == "insufficient_evidence"
    assert_capsule(bounded.capsule, bounded_request)


def test_exact_tokenizer_injection_and_unknown_tokenizer_fallback(
    repository: VaultRepository, tmp_path: Path
) -> None:
    store(
        repository,
        draft(
            "vault://global/tokenizer/item",
            title="Tokenizer",
            body="tokenizer-marker",
        ),
    )
    exact = Level0RetrievalService(
        repository,
        tmp_path / "exact-index.db",
        clock=lambda: NOW,
        token_counters={"test:unicode-codepoint-v1": len},
    )
    exact_request = request("tokenizer-marker", tokenizer="test:unicode-codepoint-v1")
    exact_result = exact.retrieve(exact_request)
    assert exact_result.capsule is not None
    assert exact_result.capsule["budget"]["method"] == "exact_tokenizer"
    assert exact_result.capsule["budget"]["guarantee"] == "exact_tokens"
    assert exact_result.capsule["budget"]["used"] == len(
        accounting_payload(exact_result.capsule).decode("utf-8")
    )

    unknown_request = request("tokenizer-marker", tokenizer="unknown:v1")
    unknown_result = exact.retrieve(unknown_request)
    assert unknown_result.capsule is not None
    assert unknown_result.capsule["budget"]["method"] == "utf8_bytes"
    assert unknown_result.capsule["budget"]["guarantee"] == "hard_bytes"
    assert "tokenizer_id" not in unknown_result.capsule["budget"]


def test_minimum_failed_capsule_boundary_and_too_small_protocol_error(
    service: Level0RetrievalService,
) -> None:
    initial_request = request("no-match", max_bytes=20_000)
    initial = service.retrieve(initial_request)
    assert initial.capsule is not None
    fallback = service.retrieve(
        request("no-match", max_bytes=initial.capsule["budget"]["used"] - 1)
    )
    assert fallback.capsule is not None
    minimum = fallback.capsule["budget"]["used"]

    boundary_request = request("no-match", max_bytes=minimum)
    boundary = service.retrieve(boundary_request)
    assert boundary.capsule is not None
    assert boundary.capsule["budget"]["used"] == minimum
    assert_capsule(boundary.capsule, boundary_request)

    too_small = service.retrieve(request("no-match", max_bytes=1))
    assert too_small.capsule is None
    assert too_small.error is not None
    ERROR_VALIDATOR.validate(too_small.error)
    assert too_small.error["error"]["code"] == "budget_too_small_for_capsule"
    assert too_small.error["accounting"]["minimum_required"] > 1


def test_exact_tokenizer_minimum_failed_boundary(
    tmp_path: Path, repository: VaultRepository
) -> None:
    service = Level0RetrievalService(
        repository,
        tmp_path / "exact-minimum.db",
        clock=lambda: NOW,
        token_counters={"test:unicode-codepoint-v1": len},
    )
    initial = service.retrieve(
        request("no-match", tokenizer="test:unicode-codepoint-v1")
    )
    assert initial.capsule is not None
    minimum = initial.capsule["budget"]["used"]

    fallback = service.retrieve(
        request(
            "no-match",
            tokenizer="test:unicode-codepoint-v1",
            max_tokens=minimum - 1,
        )
    )
    assert fallback.capsule is not None
    canonical_minimum = fallback.capsule["budget"]["used"]

    error = service.retrieve(
        request(
            "no-match",
            tokenizer="test:unicode-codepoint-v1",
            max_tokens=canonical_minimum - 1,
        )
    )

    assert error.error is not None
    ERROR_VALIDATOR.validate(error.error)
    assert error.error["accounting"]["method"] == "exact_tokenizer"


def test_deleted_or_missing_index_rebuilds_without_affecting_vault_writes(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    first = store(
        repository,
        draft(
            "vault://global/index/first",
            title="First index item",
            body="index-marker",
        ),
    )
    before = service.retrieve(request("index-marker"))
    assert before.capsule is not None
    service.index.database_path.unlink()

    candidate_draft = draft(
        "vault://global/index/candidate",
        title="Candidate after deletion",
        body="write-path-marker",
    )
    candidate = repository.create_candidate(candidate_draft, actor="writer")
    repository.update_candidate(
        replace(candidate_draft, title="Updated after deletion"),
        expected_revision=candidate.revision,
        actor="writer",
    )
    after = service.retrieve(request("index-marker"))

    assert repository.get_knowledge(first.knowledge_ref) == first
    assert after.capsule is not None
    assert selected_uris(after.capsule) == [first.knowledge_ref]
    assert after.diagnostics.index_rebuilt is True


def test_missing_fts_table_is_rebuilt_with_ranking_parity(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    for suffix in ("a", "b"):
        store(
            repository,
            draft(
                f"vault://global/rebuild/{suffix}",
                title=f"Rebuild {suffix}",
                body="rebuild-marker",
            ),
        )
    request_value = request("rebuild-marker")
    before = service.retrieve(request_value)
    assert before.capsule is not None
    with sqlite3.connect(service.index.database_path) as connection:
        connection.execute("DROP TABLE retrieval_fts")
    after = service.retrieve(request_value)

    assert after.capsule is not None
    assert after.diagnostics.index_rebuilt is True
    assert selected_uris(after.capsule) == selected_uris(before.capsule)


def test_corrupt_index_file_is_replaced_from_canonical_state(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    record = store(
        repository,
        draft(
            "vault://global/index/corrupt",
            title="Corrupt index",
            body="corrupt-index-marker",
        ),
    )
    before = service.retrieve(request("corrupt-index-marker"))
    assert before.capsule is not None
    service.index.database_path.write_bytes(b"not a sqlite database")

    after = service.retrieve(request("corrupt-index-marker"))

    assert after.capsule is not None
    assert selected_uris(after.capsule) == [record.knowledge_ref]
    assert after.diagnostics.index_rebuilt is True


def test_incompatible_index_schema_is_replaced(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    record = store(
        repository,
        draft(
            "vault://global/index/incompatible",
            title="Incompatible index",
            body="incompatible-index-marker",
        ),
    )
    service.retrieve(request("incompatible-index-marker"))
    with sqlite3.connect(service.index.database_path) as connection:
        connection.execute("DROP TABLE retrieval_fts")
        connection.execute(
            """
            CREATE TABLE retrieval_fts (
                knowledge_ref TEXT,
                title TEXT,
                tags TEXT,
                topics TEXT,
                body TEXT
            )
            """
        )

    result = service.retrieve(request("incompatible-index-marker"))

    assert result.capsule is not None
    assert selected_uris(result.capsule) == [record.knowledge_ref]
    assert result.diagnostics.index_rebuilt is True


def test_index_loss_between_sync_and_search_rebuilds_once(
    repository: VaultRepository,
    service: Level0RetrievalService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = store(
        repository,
        draft(
            "vault://global/index/race",
            title="Index race",
            body="index-race-marker",
        ),
    )
    synchronize = service.index.synchronize

    def synchronize_then_delete(records: list[KnowledgeRecord]) -> Any:
        sync = synchronize(records)
        service.index.database_path.unlink()
        return sync

    monkeypatch.setattr(service.index, "synchronize", synchronize_then_delete)

    result = service.retrieve(request("index-race-marker"))

    assert result.capsule is not None
    assert selected_uris(result.capsule) == [record.knowledge_ref]
    assert result.diagnostics.index_rebuilt is True


def test_no_match_is_truthful_failed_capsule(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    store(
        repository,
        draft(
            "vault://global/no-match/item",
            title="Existing item",
            body="unrelated content",
        ),
    )
    request_value = request("absent-term")

    result = service.retrieve(request_value)

    assert result.capsule is not None
    assert result.capsule["status"] == "failed"
    assert result.capsule["context"] == ""
    assert result.capsule["evidence"] == []
    assert result.capsule["unresolved"][0]["kind"] == "insufficient_evidence"
    assert_capsule(result.capsule, request_value)


def test_undefined_conditions_fail_closed_without_semantic_interpretation(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    record = store(
        repository,
        draft(
            "vault://global/conditions/opaque",
            title="Opaque conditions",
            body="opaque-condition-marker",
            applies_when={"natural_language": "maybe when appropriate"},
            counterconditions=[{"unknown_operator": {"x": 1}}],
        ),
    )

    result = service.retrieve(request("opaque-condition-marker"))

    assert result.capsule is not None
    assert result.capsule["status"] == "failed"
    assert selected_uris(result.capsule) == []
    assert result.capsule["critical_facts"] == []
    assert result.capsule["constraints"] == []
    assert result.capsule["pitfalls"] == []
    assert result.diagnostics.excluded_applicability_count == 1
    assert repository.get_knowledge(record.knowledge_ref) == record


def test_diagnostics_are_separate_from_strict_capsule_and_measure_baseline(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    store(
        repository,
        draft(
            "vault://global/diagnostics/item",
            title="Diagnostics",
            body="diagnostics-marker",
        ),
    )
    request_value = request("diagnostics-marker")

    result = service.retrieve(request_value)

    assert result.capsule is not None
    assert_capsule(result.capsule, request_value)
    forbidden = {
        "candidate_count",
        "selected_count",
        "excluded_scope_count",
        "excluded_lifecycle_count",
        "excluded_applicability_count",
        "excluded_stale_count",
        "malformed_freshness_count",
        "index_rebuilt",
        "index_watermark",
    }
    assert forbidden.isdisjoint(result.capsule)
    assert result.diagnostics.candidate_count == 1
    assert result.diagnostics.selected_count == 1
    assert result.diagnostics.serialized_bytes == len(
        accounting_payload(result.capsule)
    )
    assert result.diagnostics.elapsed_ms == pytest.approx(4.0)


def test_every_protocol_artifact_validates_against_schema(
    repository: VaultRepository, service: Level0RetrievalService
) -> None:
    store(
        repository,
        draft(
            "vault://global/schema/item",
            title="Schema artifact",
            body="schema-marker",
        ),
    )
    requests = [
        request("schema-marker"),
        request("missing"),
        request("schema-marker", max_bytes=1),
    ]
    for request_value in requests:
        result = service.retrieve(request_value)
        if result.capsule is not None:
            assert_capsule(result.capsule, request_value)
        else:
            assert result.error is not None
            ERROR_VALIDATOR.validate(result.error)
