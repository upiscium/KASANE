"""Independent semantic-candidate evaluation over the Level 0 golden corpus."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from kasane.retrieval import (
    DeterministicEmbeddingProvider,
    SemanticCandidateService,
)
from kasane.retrieval.request import parse_retrieval_request
from kasane.retrieval.semantic_index import DerivedSemanticIndex
from kasane.vault.models import KnowledgeDraft, KnowledgeStatus
from kasane.vault.repository import VaultRepository

JsonObject = dict[str, Any]


def generate_semantic_report(
    fixture_path: str | Path,
    query_path: str | Path,
    workspace: str | Path,
) -> JsonObject:
    """Return semantic recall without invoking or comparing Level 0 ranking."""
    fixture = _load(fixture_path)
    golden = _load(query_path)
    if fixture.get("dataset_version") != golden.get("dataset_version"):
        raise ValueError("fixture and golden query dataset versions must match")
    now = datetime.fromisoformat(str(fixture["evaluation_now"]))
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    repository = VaultRepository(
        root / "vault.db", clock=lambda: str(fixture["evaluation_now"])
    )
    _store_records(repository, fixture["records"])
    provider = DeterministicEmbeddingProvider()
    service = SemanticCandidateService(
        DerivedSemanticIndex(
            root / "semantic-index.db",
            provider,
            canonical_database_path=root / "vault.db",
        )
    )
    records = repository.list_knowledge()
    identity = {
        "provider_id": provider.provider_id,
        "model_id": provider.model_id,
        "embedding_dimension": provider.embedding_dimension,
    }
    per_query: list[JsonObject] = []
    for item in golden["queries"]:
        request = parse_retrieval_request(copy.deepcopy(item["request"]))
        result = service.generate(records, request.query, request.scope, now)
        candidates = [
            {
                "knowledge_ref": candidate.knowledge_ref,
                "semantic_score": candidate.semantic_score,
                **identity,
            }
            for candidate in result.candidates
        ]
        refs = [str(candidate["knowledge_ref"]) for candidate in candidates]
        expected = [str(ref) for ref in item.get("expected_relevant_refs", [])]
        excluded = [str(ref) for ref in item.get("expected_ineligible_refs", [])]
        per_query.append(
            {
                "query_id": str(item["query_id"]),
                "expected_relevant_refs": expected,
                "expected_ineligible_refs": excluded,
                "candidate_refs": refs,
                "candidates": candidates,
                "recovered_relevant_refs": [ref for ref in refs if ref in expected],
                "ineligible_candidate_refs": [ref for ref in refs if ref in excluded],
                "recall": _recall(refs, expected),
                "index_rebuilt": result.synchronization.rebuilt,
                "index_watermark": result.synchronization.watermark,
            }
        )
    relevant = [item for item in per_query if item["expected_relevant_refs"]]
    return {
        "schema_version": "semantic-candidate-evaluation-v1",
        "level": 1,
        "dataset_version": fixture["dataset_version"],
        "implementation": "SemanticCandidateService deterministic evaluation provider",
        "provider": identity,
        "query_count": len(per_query),
        "metrics": {"mean_recall": _mean(item["recall"] for item in relevant)},
        "q08_q09": {
            item["query_id"]: item["recovered_relevant_refs"]
            for item in per_query
            if item["query_id"] in {"q08-synonym-paraphrase", "q09-abbreviation"}
        },
        "eligibility_negative_count": sum(
            len(item["ineligible_candidate_refs"]) for item in per_query
        ),
        "per_query": per_query,
    }


def write_semantic_report(report: Mapping[str, Any], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _load(path: str | Path) -> JsonObject:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain an object")
    return value


def _store_records(repository: VaultRepository, records: list[JsonObject]) -> None:
    from kasane.evaluation.level0 import _store_record

    for item in records:
        draft = KnowledgeDraft(
            knowledge_ref=str(item["knowledge_ref"]),
            knowledge_type=str(item["knowledge_type"]),
            title=str(item["title"]),
            body=str(item["body"]),
            tags=[str(tag) for tag in item.get("tags", [])],
            scope=item.get("scope", {}),
            applies_when=item.get("applies_when", {}),
            counterconditions=item.get("counterconditions", []),
            sources=item.get("sources", []),
            provenance=item.get("provenance", {}),
            stale_after=item.get("stale_after"),
        )
        _store_record(repository, draft, KnowledgeStatus(str(item["status"])))


def _recall(refs: list[str], expected: list[str]) -> float:
    return round(len(set(refs) & set(expected)) / len(expected), 6) if expected else 0.0


def _mean(values: Any) -> float:
    values = list(values)
    return round(sum(values) / len(values), 6) if values else 0.0
