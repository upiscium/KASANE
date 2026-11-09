"""Independent Level 1 evaluation; it never reads or rewrites the Level 0 baseline."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from kasane.evaluation.semantic import _load, _store_records
from kasane.retrieval import Level1RetrievalService
from kasane.retrieval.fake_embeddings import DeterministicEmbeddingProvider
from kasane.retrieval.rerank import DeterministicRerankProvider
from kasane.vault.repository import VaultRepository


def _no_match_is_correct(item: dict[str, Any]) -> bool:
    """A no-match is correct only with zero candidates and zero top-3 refs."""
    return not item.get("candidate_refs") and not item.get("top3_refs")


def generate_level1_report(
    fixture_path: str | Path, query_path: str | Path, workspace: str | Path
) -> dict[str, Any]:
    fixture = _load(fixture_path)
    golden = _load(query_path)
    if fixture.get("dataset_version") != golden.get("dataset_version"):
        raise ValueError("fixture and golden query dataset versions must match")
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    repository = VaultRepository(
        root / "vault.db", clock=lambda: str(fixture["evaluation_now"])
    )
    _store_records(repository, fixture["records"])
    service = Level1RetrievalService(
        repository,
        semantic_index_path=root / "semantic.db",
        embedding_provider=DeterministicEmbeddingProvider(),
        rerank_provider=DeterministicRerankProvider(),
    )
    per_query: list[dict[str, Any]] = []
    for item in golden["queries"]:
        request = copy.deepcopy(item["request"])
        request["mode"] = "auto"
        result = service.retrieve(request)
        refs = [entry["uri"] for entry in result.artifact.get("knowledge_refs", [])]
        expected = [str(ref) for ref in item.get("expected_relevant_refs", [])]
        ineligible = {str(ref) for ref in item.get("expected_ineligible_refs", [])}
        top3 = refs[:3]
        recovered = [ref for ref in top3 if ref in expected]
        first_rank = next(
            (index + 1 for index, ref in enumerate(top3) if ref in expected), None
        )
        per_query.append(
            {
                "query_id": str(item["query_id"]),
                "query_class": str(item.get("query_class", "")),
                "expected_relevant_refs": expected,
                "expected_ineligible_refs": sorted(ineligible),
                "expected_no_match": bool(item.get("expected_no_match", False)),
                "candidate_refs": refs,
                "top3_refs": top3,
                "recovered_relevant_refs": recovered,
                "recall_at_3": len(recovered) / len(expected) if expected else None,
                "reciprocal_rank": 1 / first_rank if first_rank else 0.0,
                "level": result.diagnostics.level,
                "path": list(result.diagnostics.path),
                "fallback": result.diagnostics.level == 0,
                "gate_violation": bool(set(refs) & ineligible),
                "caller_burden": result.diagnostics.level > 0,
                "status": result.artifact.get("status", "protocol_error"),
                "terminal_reason": result.artifact.get("retrieval", {}).get(
                    "terminal_reason"
                ),
                "candidate_count": result.diagnostics.candidate_count,
                "selected_count": result.diagnostics.selected_count,
                "lexical_candidate_count": result.diagnostics.lexical_candidate_count,
                "semantic_candidate_count": result.diagnostics.semantic_candidate_count,
            }
        )
    relevant = [x for x in per_query if x["expected_relevant_refs"]]
    no_match = [item for item in golden["queries"] if item.get("expected_no_match")]
    by_id = {x["query_id"]: x for x in per_query}
    no_match_correct = sum(
        _no_match_is_correct(by_id.get(str(item["query_id"]), {})) for item in no_match
    )
    return {
        "schema_version": "level1-evaluation-v1",
        "level": 1,
        "dataset_version": fixture["dataset_version"],
        "query_count": len(per_query),
        "metric_definitions": {
            "recall_at_3": "Mean fraction of expected relevant references in the first three returned references, over queries with expected relevant references.",
            "mrr": "Mean reciprocal rank of the first expected relevant reference in the first three returned references, over queries with expected relevant references.",
            "no_match_accuracy": "Fraction of expected-no-match queries returning zero candidate references and zero top-3 references.",
            "gate_violations": "Count of queries whose returned references intersect expected ineligible references.",
            "caller_burden": "Count of queries whose diagnostics level is greater than zero.",
        },
        "metrics": {
            "queries": len(relevant),
            "recall_at_3": sum(x["recall_at_3"] or 0 for x in relevant) / len(relevant)
            if relevant
            else 0.0,
            "mrr": sum(x["reciprocal_rank"] for x in relevant) / len(relevant)
            if relevant
            else 0.0,
            "no_match_accuracy": no_match_correct / len(no_match) if no_match else 1.0,
            "gate_violations": sum(x["gate_violation"] for x in per_query),
            "caller_burden": sum(x["caller_burden"] for x in per_query),
        },
        "per_query": per_query,
    }


def write_level1_report(report: dict[str, Any], output_path: str | Path) -> None:
    Path(output_path).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
