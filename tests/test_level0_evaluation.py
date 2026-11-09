from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kasane.evaluation.level0 import (
    classify_failures,
    generate_level0_report,
    no_match_correct,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    write_report,
)

ROOT = Path(__file__).parents[1]
EVALUATION_DIR = ROOT / "evaluation" / "level0"
FIXTURE = EVALUATION_DIR / "knowledge-fixture.json"
QUERIES = EVALUATION_DIR / "golden-queries.json"
PROFILES = EVALUATION_DIR / "budget-profiles.json"
BASELINE = EVALUATION_DIR / "baseline.json"


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_golden_corpus_is_synthetic_machine_readable_and_complete() -> None:
    fixture = load(FIXTURE)
    golden = load(QUERIES)
    records = fixture["records"]
    queries = golden["queries"]

    assert fixture["dataset_version"] == golden["dataset_version"]
    assert len(records) == 28
    assert len(queries) == 16
    assert len({item["query_class"] for item in queries}) >= 15
    assert len({item["query_id"] for item in queries}) == len(queries)
    assert all(
        not item["knowledge_ref"].startswith("vault://private/") for item in records
    )
    assert all(
        {
            "query_id",
            "query_class",
            "request",
            "expected_relevant_refs",
            "expected_no_match",
            "notes",
        }
        <= set(item)
        for item in queries
    )


def test_report_generation_is_deterministic_and_matches_snapshot(
    tmp_path: Path,
) -> None:
    first = generate_level0_report(FIXTURE, QUERIES, PROFILES, tmp_path / "first")
    second = generate_level0_report(FIXTURE, QUERIES, PROFILES, tmp_path / "second")

    baseline = load(BASELINE)
    assert first == second

    # The snapshot pins behavior *and* implementation provenance (#37): any
    # change to the hashed retrieval files must refresh baseline.json in the
    # same change, so the hash can never silently drift again.
    assert first == baseline, (
        "Level 0 baseline is stale: regenerate evaluation/level0/baseline.json "
        "(python -m kasane.evaluation.level0 --output ...) and review the diff"
    )
    assert "latency" not in json.dumps(first).casefold()
    assert "elapsed" not in json.dumps(first).casefold()

    output = tmp_path / "report.json"
    write_report(first, output)
    written = load(output)
    assert written == baseline


def test_level0_baseline_file_bytes_are_stable_during_evaluation(
    tmp_path: Path,
) -> None:
    before = BASELINE.read_bytes()
    generate_level0_report(FIXTURE, QUERIES, PROFILES, tmp_path / "workspace")
    assert BASELINE.read_bytes() == before


def test_required_metrics_and_profiles_are_reported() -> None:
    report = load(BASELINE)
    required_metrics = {
        "recall_at_1",
        "recall_at_3",
        "precision_at_1",
        "precision_at_3",
        "mrr",
        "no_match_accuracy",
        "mean_selected_count",
        "mean_capsule_serialized_bytes",
        "mean_exact_token_count",
        "max_exact_token_count",
    }

    assert set(report["profiles"]) == {"small", "normal", "large"}
    query_ids = None
    for profile in report["profiles"].values():
        assert required_metrics <= set(profile["metrics"])
        assert profile["tokenizer_id"] == "test:unicode-codepoint-v1"
        assert profile["metrics"]["mean_exact_token_count"] > 0
        assert profile["metrics"]["max_exact_token_count"] > 0
        current = [item["query_id"] for item in profile["per_query"]]
        query_ids = current if query_ids is None else query_ids
        assert current == query_ids


def test_ranking_fixture_has_reranker_actionable_semantic_signal() -> None:
    fixture = load(FIXTURE)
    golden = load(QUERIES)
    records = {item["knowledge_ref"]: item for item in fixture["records"]}
    query = next(
        item
        for item in golden["queries"]
        if item["query_id"] == "q15-ranking-sensitive"
    )
    ranking_refs = [
        "vault://global/ranking/a-distractor",
        "vault://global/ranking/b-distractor",
        "vault://global/ranking/c-distractor",
        "vault://global/ranking/z-authoritative",
    ]
    target_ref = query["expected_relevant_refs"][0]

    assert query["request"]["query"] == "rankingsharedomega for real systems"
    assert all("rankingsharedomega" in records[ref]["body"] for ref in ranking_refs)
    assert "live customer-facing deployments" in records[target_ref]["body"]
    assert {
        context
        for ref in ranking_refs[:-1]
        for context in ("toy", "benchmark", "classroom")
        if context in records[ref]["body"]
    } == {"toy", "benchmark", "classroom"}

    normal = load(BASELINE)["profiles"]["normal"]["per_query"]
    result = next(item for item in normal if item["query_id"] == query["query_id"])
    assert target_ref in result["lexical_candidate_refs"]
    assert result["eligible_ranked_refs"].index(target_ref) >= 3


def test_metric_primitives_use_known_ranked_selections() -> None:
    expected = {"relevant-a", "relevant-b"}
    selected = ["noise", "relevant-b", "other"]

    assert recall_at_k(selected[:1], expected) == 0.0
    assert recall_at_k(selected[:3], expected) == 0.5
    assert precision_at_k(selected[:1], expected, 1) == 0.0
    assert precision_at_k(selected[:3], expected, 3) == 0.333333
    assert reciprocal_rank(selected, expected) == 0.5
    assert reciprocal_rank(["noise"], expected) == 0.0
    assert no_match_correct("insufficient_evidence", True) is True
    assert no_match_correct("insufficient_evidence", False) is False
    assert no_match_correct("sufficient", False) is True


def test_failure_classifier_uses_independent_known_sets() -> None:
    failures = classify_failures(
        expected_refs=["missing", "gated", "budgeted", "ranked-low"],
        expected_ineligible_refs=["policy-only"],
        lexical_candidate_refs=[
            "gated",
            "budgeted",
            "ranked-low",
            "policy-only",
            "noise",
        ],
        eligible_refs={"budgeted", "ranked-low", "noise"},
        ranked_refs=["noise", "budgeted", "other", "third", "ranked-low"],
        selected_refs=["noise"],
    )

    assert failures == {
        "candidate_generation": ["missing"],
        "ranking_outside_top3": ["ranked-low"],
        "budget_or_evidence_cap": ["budgeted"],
        "eligibility_gate": ["gated", "policy-only"],
    }


def test_failure_classes_separate_candidate_ranking_budget_and_eligibility() -> None:
    report = load(BASELINE)
    analysis = report["analysis"]

    assert analysis["candidate_generation_failure_queries"] == [
        "q08-synonym-paraphrase",
        "q09-abbreviation",
    ]
    assert analysis["ranking_failure_queries"] == ["q15-ranking-sensitive"]
    assert analysis["budget_failure_queries"] == []
    assert analysis["intentional_eligibility_exclusion_queries"] == [
        "q07-near-lookalike",
        "q14-eligibility-distractors",
    ]
    assert report["profiles"]["small"]["failure_counts"]["budget_or_evidence_cap"] == 1
    assert report["profiles"]["normal"]["failure_counts"] == {
        "budget_or_evidence_cap": 0,
        "candidate_generation": 2,
        "eligibility_gate": 6,
        "ranking_outside_top3": 1,
    }


def test_budget_profiles_expose_size_and_recall_tradeoff() -> None:
    report = load(BASELINE)
    small = report["profiles"]["small"]["metrics"]
    normal = report["profiles"]["normal"]["metrics"]
    large = report["profiles"]["large"]["metrics"]

    assert small["recall_at_3"] < normal["recall_at_3"] == large["recall_at_3"]
    assert (
        small["mean_selected_count"]
        < normal["mean_selected_count"]
        < large["mean_selected_count"]
    )
    assert (
        small["mean_capsule_serialized_bytes"]
        < normal["mean_capsule_serialized_bytes"]
        < large["mean_capsule_serialized_bytes"]
    )


def test_level1_recommendation_is_derived_from_both_failure_types() -> None:
    report = load(BASELINE)
    recommendation = report["level1_recommendation"]

    assert recommendation["direction"] == "C. hybrid"
    assert "2 candidate-generation misses" in recommendation["basis"]
    assert "1 ranking miss" in recommendation["basis"]
