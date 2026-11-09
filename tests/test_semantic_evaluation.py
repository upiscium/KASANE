from __future__ import annotations

import hashlib
import json
from pathlib import Path

from kasane.evaluation.semantic import generate_semantic_report
from kasane.retrieval import DeterministicEmbeddingProvider

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "evaluation/level0/knowledge-fixture.json"
QUERIES = ROOT / "evaluation/level0/golden-queries.json"
BASELINE = ROOT / "evaluation/level0/baseline.json"


def test_deterministic_provider_identity_and_vectors_are_stable() -> None:
    provider = DeterministicEmbeddingProvider()
    assert (provider.provider_id, provider.model_id, provider.embedding_dimension) == (
        "test:deterministic-semantic",
        "test:golden-concepts-v1",
        8,
    )
    assert provider.embed_query(
        "permission check at action moment"
    ) == provider.embed_query("permission check at action moment")
    assert (
        provider.embed_query("TLS")
        == provider.embed_documents(["Transport Layer Security configuration"])[0]
    )


def test_semantic_report_recovers_q08_q09_and_excludes_ineligible_records(
    tmp_path: Path,
) -> None:
    report = generate_semantic_report(FIXTURE, QUERIES, tmp_path)
    assert report["q08_q09"] == {
        "q08-synonym-paraphrase": ["vault://global/execution/effect-time-validation"],
        "q09-abbreviation": ["vault://global/network/transport-layer-security"],
    }
    assert report["eligibility_negative_count"] == 0
    assert report["provider"] == {
        "provider_id": "test:deterministic-semantic",
        "model_id": "test:golden-concepts-v1",
        "embedding_dimension": 8,
    }
    negatives = next(
        item
        for item in report["per_query"]
        if item["query_id"] == "q14-eligibility-distractors"
    )
    assert negatives["ineligible_candidate_refs"] == []
    assert not set(negatives["expected_ineligible_refs"]) & set(
        negatives["candidate_refs"]
    )
    assert set(negatives["expected_ineligible_refs"]) == {
        "vault://global/eligibility/stale",
        "vault://global/eligibility/candidate",
        "vault://global/eligibility/verified",
        "vault://global/eligibility/deprecated",
        "vault://global/eligibility/conditional",
    }
    scope_negative = next(
        item for item in report["per_query"] if item["query_id"] == "q07-near-lookalike"
    )
    assert scope_negative["ineligible_candidate_refs"] == []
    assert (
        "vault://project/terreatex/scope-lookalike"
        not in scope_negative["candidate_refs"]
    )


def test_level0_baseline_artifact_is_not_modified() -> None:
    baseline = BASELINE.read_bytes()
    # Approved digest protects the checked-in evaluation contract; comparing a
    # file to itself is vacuous.
    assert (
        hashlib.sha256(baseline).hexdigest()
        == "d987a6e7e2a82a1b4074306484045d17fb767434e1d1f9f8fd1a1281b89fa48e"
    )
    assert json.loads(baseline)["schema_version"] == "level0-evaluation-v1"
