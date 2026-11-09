from __future__ import annotations

import hashlib
import json
from pathlib import Path

from kasane.evaluation.level1 import (
    _no_match_is_correct,
    generate_level1_report,
    write_level1_report,
)

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "evaluation/level0/knowledge-fixture.json"
QUERIES = ROOT / "evaluation/level0/golden-queries.json"
REPORT = ROOT / "evaluation/level1/report.json"
BASELINE = ROOT / "evaluation/level0/baseline.json"


def test_level1_report_is_reproducibly_generated_with_query_evidence(
    tmp_path: Path,
) -> None:
    report = generate_level1_report(FIXTURE, QUERIES, tmp_path)
    generated = tmp_path / "level1-report.json"
    write_level1_report(report, generated)
    assert json.loads(generated.read_text(encoding="utf-8")) == json.loads(
        REPORT.read_text(encoding="utf-8")
    )
    assert report["query_count"] == len(report["per_query"]) == 16
    assert set(report["metric_definitions"]) == set(report["metrics"]) - {"queries"}
    assert report["metrics"]["gate_violations"] == 0
    assert report["metrics"]["no_match_accuracy"] == 1.0
    by_id = {item["query_id"]: item for item in report["per_query"]}
    assert by_id["q08-synonym-paraphrase"]["top3_refs"][:1] == [
        "vault://global/execution/effect-time-validation"
    ]
    assert by_id["q09-abbreviation"]["top3_refs"][:1] == [
        "vault://global/network/transport-layer-security"
    ]
    assert by_id["q15-ranking-sensitive"]["level"] == 1
    assert by_id["q15-ranking-sensitive"]["top3_refs"][0] == (
        "vault://global/ranking/z-authoritative"
    )
    assert all(
        "candidate_refs" in item and "top3_refs" in item for item in report["per_query"]
    )


def test_level1_preserves_every_level0_strong_query_class() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    strong_classes = set(baseline["analysis"]["strong_query_classes"])
    level0 = {
        item["query_class"]: item
        for item in baseline["profiles"]["normal"]["per_query"]
    }
    level1 = {item["query_class"]: item for item in report["per_query"]}

    assert strong_classes <= level0.keys()
    assert strong_classes <= level1.keys()
    for query_class in strong_classes:
        expected = level0[query_class]
        actual = level1[query_class]
        if expected["expected_no_match"]:
            assert _no_match_is_correct(actual)
        else:
            assert actual["recall_at_3"] >= expected["recall_at_3"]
            assert actual["reciprocal_rank"] >= expected["reciprocal_rank"]


def test_level0_baseline_is_not_modified_by_level1_generation() -> None:
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == (
        "d987a6e7e2a82a1b4074306484045d17fb767434e1d1f9f8fd1a1281b89fa48e"
    )


def test_injected_unrelated_result_is_not_a_no_match() -> None:
    assert not _no_match_is_correct(
        {"candidate_refs": ["vault://global/unrelated"], "top3_refs": []}
    )


def test_no_match_requires_zero_candidates_and_zero_top3_references() -> None:
    assert _no_match_is_correct({"candidate_refs": [], "top3_refs": []})
    assert not _no_match_is_correct(
        {"candidate_refs": [], "top3_refs": ["vault://global/unrelated"]}
    )
