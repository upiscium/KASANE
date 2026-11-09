"""Reproducible golden-query evaluation for unchanged Level 0 retrieval."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from kasane.retrieval.request import parse_retrieval_request
from kasane.retrieval.service import Level0RetrievalService
from kasane.vault.models import (
    KnowledgeDraft,
    KnowledgeRecord,
    KnowledgeStatus,
    VerificationOutcome,
)
from kasane.vault.repository import VaultRepository

JsonObject = dict[str, Any]
TOP_K = 3
TEST_TOKENIZER_ID = "test:unicode-codepoint-v1"


def load_json_object(path: str | Path) -> JsonObject:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def generate_level0_report(
    fixture_path: str | Path,
    query_path: str | Path,
    profile_path: str | Path,
    workspace: str | Path,
) -> JsonObject:
    """Evaluate current behavior without modifying the production algorithm."""

    fixture = load_json_object(fixture_path)
    golden = load_json_object(query_path)
    profile_document = load_json_object(profile_path)
    records = _object_list(fixture, "records")
    queries = _object_list(golden, "queries")
    profiles = _object_mapping(profile_document, "profiles")
    _validate_corpus(fixture, golden, profile_document, records, queries, profiles)

    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    profile_reports: dict[str, Any] = {}
    for profile_name in sorted(profiles):
        profile_reports[profile_name] = _evaluate_profile(
            profile_name=profile_name,
            profile=_object(profiles[profile_name], f"profiles.{profile_name}"),
            tokenizer=_object(profile_document["tokenizer"], "tokenizer"),
            fixture=fixture,
            records=records,
            queries=queries,
            workspace=workspace_path / profile_name,
        )

    normal_failures = profile_reports["normal"]["failure_counts"]
    recommendation = _recommend_level1(normal_failures)
    normal_queries = profile_reports["normal"]["per_query"]
    return {
        "schema_version": "level0-evaluation-v1",
        "level": 0,
        "dataset_version": fixture["dataset_version"],
        "profile_version": profile_document["profile_version"],
        "implementation": "Level0RetrievalService issue-6 baseline",
        "implementation_sha256": _implementation_sha256(),
        "fixture_sha256": _canonical_sha256(fixture),
        "queries_sha256": _canonical_sha256(golden),
        "profiles_sha256": _canonical_sha256(profile_document),
        "query_count": len(queries),
        "query_classes": sorted({str(item["query_class"]) for item in queries}),
        "profiles": profile_reports,
        "analysis": {
            "strong_query_classes": _strong_classes(normal_queries),
            "weak_query_classes": _weak_classes(normal_queries),
            "candidate_generation_failure_queries": _query_ids_with_failure(
                normal_queries, "candidate_generation"
            ),
            "ranking_failure_queries": _query_ids_with_failure(
                normal_queries, "ranking_outside_top3"
            ),
            "budget_failure_queries": _query_ids_with_failure(
                normal_queries, "budget_or_evidence_cap"
            ),
            "intentional_eligibility_exclusion_queries": _query_ids_with_failure(
                normal_queries, "eligibility_gate"
            ),
        },
        "level1_recommendation": recommendation,
    }


def write_report(report: Mapping[str, Any], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _evaluate_profile(
    *,
    profile_name: str,
    profile: JsonObject,
    tokenizer: JsonObject,
    fixture: JsonObject,
    records: list[JsonObject],
    queries: list[JsonObject],
    workspace: Path,
) -> JsonObject:
    repository = VaultRepository(
        workspace / "vault.db",
        clock=lambda: str(fixture["evaluation_now"]),
    )
    _store_records(repository, records)
    tokenizer_id = str(tokenizer["id"])
    service = Level0RetrievalService(
        repository,
        workspace / "level0-index.db",
        clock=lambda: datetime.fromisoformat(str(fixture["evaluation_now"])),
        token_counters={tokenizer_id: len},
        monotonic=lambda: 0.0,
    )

    per_query = [
        _evaluate_query(service, query, profile, tokenizer_id) for query in queries
    ]
    relevant_queries = [item for item in per_query if item["expected_relevant_refs"]]
    exact_counts = [
        int(item["exact_token_count"])
        for item in per_query
        if item["exact_token_count"] is not None
    ]
    failure_counts = {
        failure: sum(len(item["failure_classification"][failure]) for item in per_query)
        for failure in (
            "candidate_generation",
            "ranking_outside_top3",
            "budget_or_evidence_cap",
            "eligibility_gate",
        )
    }
    return {
        "profile": profile_name,
        "budget": copy.deepcopy(profile),
        "tokenizer_id": tokenizer_id,
        "query_count": len(per_query),
        "metrics": {
            "recall_at_1": _rounded_mean(
                [float(item["recall_at_1"]) for item in relevant_queries]
            ),
            "recall_at_3": _rounded_mean(
                [float(item["recall_at_3"]) for item in relevant_queries]
            ),
            "precision_at_1": _rounded_mean(
                [float(item["precision_at_1"]) for item in relevant_queries]
            ),
            "precision_at_3": _rounded_mean(
                [float(item["precision_at_3"]) for item in relevant_queries]
            ),
            "mrr": _rounded_mean(
                [float(item["reciprocal_rank"]) for item in relevant_queries]
            ),
            "no_match_accuracy": _rounded_mean(
                [1.0 if item["no_match_correct"] else 0.0 for item in per_query]
            ),
            "mean_selected_count": _rounded_mean(
                [float(item["selected_count"]) for item in per_query]
            ),
            "mean_capsule_serialized_bytes": _rounded_mean(
                [float(item["capsule_serialized_bytes"]) for item in per_query]
            ),
            "mean_exact_token_count": _rounded_mean(
                [float(value) for value in exact_counts]
            ),
            "max_exact_token_count": max(exact_counts, default=0),
            "unsupported_query_count": sum(
                1 for item in relevant_queries if not item["selected_relevant_refs"]
            ),
            "wrong_selection_count": sum(
                len(item["wrong_selected_refs"]) for item in per_query
            ),
        },
        "failure_counts": failure_counts,
        "per_query": per_query,
    }


def _evaluate_query(
    service: Level0RetrievalService,
    query: JsonObject,
    profile: JsonObject,
    tokenizer_id: str,
) -> JsonObject:
    request_value = copy.deepcopy(_object(query["request"], "query.request"))
    request_value["budget"] = copy.deepcopy(profile)
    request_value["tokenizer"] = {"id": tokenizer_id}
    parsed = parse_retrieval_request(request_value)

    records = service.repository.list_knowledge()
    service.index.synchronize(records)
    lexical_hits = service.index.search(parsed.query)
    lexical_refs = [hit.knowledge_ref for hit in lexical_hits]
    eligible, _ = service._eligible_records(records, parsed)
    eligible_refs = {record.knowledge_ref for record in eligible}
    hits = {hit.knowledge_ref: hit for hit in lexical_hits}
    ranked = service._rank(eligible, hits, parsed)
    ranked_refs = [item.knowledge_ref for item in ranked]

    result = service.retrieve(request_value)
    capsule = result.capsule
    selected_refs = (
        [str(item["uri"]) for item in capsule["knowledge_refs"]]
        if capsule is not None
        else []
    )
    expected_refs = [str(item) for item in _list(query, "expected_relevant_refs")]
    ineligible_refs = [str(item) for item in query.get("expected_ineligible_refs", [])]
    expected = set(expected_refs)
    selected_relevant = [ref for ref in selected_refs if ref in expected]
    wrong_selected = [ref for ref in selected_refs if ref not in expected]
    failure_classification = classify_failures(
        expected_refs=expected_refs,
        expected_ineligible_refs=ineligible_refs,
        lexical_candidate_refs=lexical_refs,
        eligible_refs=eligible_refs,
        ranked_refs=ranked_refs,
        selected_refs=selected_refs,
    )

    terminal_reason = (
        str(capsule["retrieval"]["terminal_reason"])
        if capsule is not None
        else "protocol_error"
    )
    reported_no_match = is_reported_no_match(terminal_reason)
    expected_no_match = bool(query["expected_no_match"])
    top1 = selected_refs[:1]
    top3 = selected_refs[:TOP_K]
    capsule_bytes = int(capsule["budget"]["serialized_bytes"]) if capsule else 0
    exact_tokens = (
        int(capsule["budget"]["used"])
        if capsule and capsule["budget"]["method"] == "exact_tokenizer"
        else None
    )
    return {
        "query_id": str(query["query_id"]),
        "query_class": str(query["query_class"]),
        "expected_relevant_refs": expected_refs,
        "expected_no_match": expected_no_match,
        "lexical_candidate_refs": lexical_refs,
        "eligible_ranked_refs": ranked_refs,
        "selected_refs": selected_refs,
        "selected_relevant_refs": selected_relevant,
        "wrong_selected_refs": wrong_selected,
        "recall_at_1": recall_at_k(top1, expected),
        "recall_at_3": recall_at_k(top3, expected),
        "precision_at_1": precision_at_k(top1, expected, 1),
        "precision_at_3": precision_at_k(top3, expected, TOP_K),
        "reciprocal_rank": reciprocal_rank(selected_refs, expected),
        "reported_no_match": reported_no_match,
        "no_match_correct": no_match_correct(terminal_reason, expected_no_match),
        "status": str(capsule["status"]) if capsule else "protocol_error",
        "terminal_reason": terminal_reason,
        "selected_count": len(selected_refs),
        "capsule_serialized_bytes": capsule_bytes,
        "exact_token_count": exact_tokens,
        "failure_classification": failure_classification,
    }


def seed_fixture_vault(fixture_path: str | Path, vault_path: str | Path) -> int:
    """Create a new dev Vault from the Level 0 fixture; return canonical count.

    Refuses to touch an existing file so a real Vault is never mixed with
    synthetic fixture knowledge.
    """

    target = Path(vault_path)
    if target.exists():
        raise FileExistsError(f"refusing to seed existing Vault: {target}")
    records = _object_list(load_json_object(fixture_path), "records")
    _store_records(VaultRepository(target), records)
    return sum(1 for item in records if item["status"] == "canonical")


def _store_records(repository: VaultRepository, records: list[JsonObject]) -> None:
    for item in records:
        status = KnowledgeStatus(str(item["status"]))
        draft = KnowledgeDraft(
            knowledge_ref=str(item["knowledge_ref"]),
            knowledge_type=str(item["knowledge_type"]),
            title=str(item["title"]),
            body=str(item["body"]),
            tags=[str(tag) for tag in item.get("tags", [])],
            scope=copy.deepcopy(item.get("scope", {})),
            applies_when=copy.deepcopy(item.get("applies_when", {})),
            counterconditions=copy.deepcopy(item.get("counterconditions", [])),
            sources=copy.deepcopy(
                item.get(
                    "sources",
                    [
                        {
                            "kind": "repository",
                            "uri": f"fixture://{item['knowledge_ref']}",
                        }
                    ],
                )
            ),
            provenance=copy.deepcopy(item.get("provenance", {})),
            stale_after=(
                str(item["stale_after"])
                if item.get("stale_after") is not None
                else None
            ),
        )
        _store_record(repository, draft, status)


def _store_record(
    repository: VaultRepository, draft: KnowledgeDraft, status: KnowledgeStatus
) -> KnowledgeRecord:
    record = repository.create_candidate(draft, actor="evaluation-fixture")
    if status is KnowledgeStatus.CANDIDATE:
        return record
    record = repository.record_verification(
        draft.knowledge_ref,
        expected_revision=record.revision,
        actor="evaluation-fixture",
        outcome=VerificationOutcome.PASSED,
        verification={"method": "synthetic-golden-fixture"},
    )
    record = repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.VERIFIED,
        expected_revision=record.revision,
        actor="evaluation-fixture",
    )
    if status is KnowledgeStatus.VERIFIED:
        return record
    record = repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.CANONICAL,
        expected_revision=record.revision,
        actor="evaluation-fixture",
    )
    if status is KnowledgeStatus.CANONICAL:
        return record
    record = repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.DEPRECATED,
        expected_revision=record.revision,
        actor="evaluation-fixture",
    )
    if status is KnowledgeStatus.DEPRECATED:
        return record
    return repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.ARCHIVED,
        expected_revision=record.revision,
        actor="evaluation-fixture",
    )


def _validate_corpus(
    fixture: JsonObject,
    golden: JsonObject,
    profile_document: JsonObject,
    records: list[JsonObject],
    queries: list[JsonObject],
    profiles: Mapping[str, Any],
) -> None:
    if fixture.get("dataset_version") != golden.get("dataset_version"):
        raise ValueError("fixture and golden query dataset versions must match")
    datetime.fromisoformat(str(fixture["evaluation_now"]))
    refs = [str(item["knowledge_ref"]) for item in records]
    if len(refs) != len(set(refs)):
        raise ValueError("Knowledge fixture refs must be unique")
    query_ids = [str(item["query_id"]) for item in queries]
    if len(query_ids) != len(set(query_ids)):
        raise ValueError("golden query IDs must be unique")
    classes = {str(item["query_class"]) for item in queries}
    if len(classes) < 15:
        raise ValueError("golden queries must cover at least 15 query classes")
    ref_set = set(refs)
    for query in queries:
        parse_retrieval_request(_object(query["request"], "query.request"))
        referenced = [
            *[str(item) for item in _list(query, "expected_relevant_refs")],
            *[str(item) for item in query.get("expected_irrelevant_refs", [])],
            *[str(item) for item in query.get("expected_ineligible_refs", [])],
        ]
        unknown = set(referenced) - ref_set
        if unknown:
            raise ValueError(
                f"golden query references unknown Knowledge: {sorted(unknown)}"
            )
    if set(profiles) != {"small", "normal", "large"}:
        raise ValueError("budget profiles must be exactly small, normal, and large")
    tokenizer = _object(profile_document["tokenizer"], "tokenizer")
    if tokenizer.get("id") != TEST_TOKENIZER_ID:
        raise ValueError("baseline requires the deterministic code-point tokenizer")
    for name, profile in profiles.items():
        value = _object(profile, f"profiles.{name}")
        request = copy.deepcopy(_object(queries[0]["request"], "query.request"))
        request["budget"] = copy.deepcopy(value)
        request["tokenizer"] = {"id": TEST_TOKENIZER_ID}
        parse_retrieval_request(request)


def _recommend_level1(failures: Mapping[str, Any]) -> JsonObject:
    candidate = int(failures["candidate_generation"])
    ranking = int(failures["ranking_outside_top3"])
    if candidate and ranking:
        direction = "C. hybrid"
        reason = (
            f"normal profile records {candidate} candidate-generation misses and "
            f"{ranking} ranking miss; expansion and reranking address distinct failures"
        )
    elif candidate:
        direction = "A. embedding/vector candidate expansion"
        reason = f"normal profile records {candidate} candidate-generation misses and no ranking miss"
    else:
        direction = "B. reranking"
        reason = f"normal profile records {ranking} ranking misses and no candidate-generation miss"
    return {"direction": direction, "basis": reason}


def _strong_classes(per_query: list[JsonObject]) -> list[str]:
    return sorted(
        {
            str(item["query_class"])
            for item in per_query
            if (
                (item["expected_relevant_refs"] and float(item["recall_at_1"]) == 1.0)
                or (item["expected_no_match"] and item["no_match_correct"])
            )
        }
    )


def _weak_classes(per_query: list[JsonObject]) -> list[str]:
    return sorted(
        {
            str(item["query_class"])
            for item in per_query
            if (
                (item["expected_relevant_refs"] and float(item["recall_at_1"]) < 1.0)
                or not item["no_match_correct"]
            )
        }
    )


def _query_ids_with_failure(per_query: list[JsonObject], failure: str) -> list[str]:
    return [
        str(item["query_id"])
        for item in per_query
        if item["failure_classification"][failure]
    ]


def recall_at_k(selected: list[str], expected: set[str]) -> float:
    if not expected:
        return 0.0
    return round(len(set(selected) & expected) / len(expected), 6)


def precision_at_k(selected: list[str], expected: set[str], k: int) -> float:
    if k < 1:
        raise ValueError("precision k must be positive")
    return round(len(set(selected) & expected) / k, 6)


def reciprocal_rank(selected: list[str], expected: set[str]) -> float:
    first_relevant_rank = next(
        (index for index, ref in enumerate(selected, start=1) if ref in expected),
        None,
    )
    return round(1.0 / first_relevant_rank, 6) if first_relevant_rank else 0.0


def is_reported_no_match(terminal_reason: str) -> bool:
    return terminal_reason == "insufficient_evidence"


def no_match_correct(terminal_reason: str, expected_no_match: bool) -> bool:
    return is_reported_no_match(terminal_reason) == expected_no_match


def classify_failures(
    *,
    expected_refs: list[str],
    expected_ineligible_refs: list[str],
    lexical_candidate_refs: list[str],
    eligible_refs: set[str],
    ranked_refs: list[str],
    selected_refs: list[str],
) -> dict[str, list[str]]:
    positions = {ref: position for position, ref in enumerate(ranked_refs, start=1)}
    lexical_set = set(lexical_candidate_refs)
    return {
        "candidate_generation": [
            ref for ref in expected_refs if ref not in lexical_set
        ],
        "ranking_outside_top3": [
            ref for ref in expected_refs if ref in positions and positions[ref] > TOP_K
        ],
        "budget_or_evidence_cap": [
            ref
            for ref in expected_refs
            if ref in positions and positions[ref] <= TOP_K and ref not in selected_refs
        ],
        "eligibility_gate": sorted(
            {
                ref
                for ref in [*expected_refs, *expected_ineligible_refs]
                if ref in lexical_set and ref not in eligible_refs
            }
        ),
    }


def _rounded_mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _canonical_sha256(value: JsonObject) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _implementation_sha256() -> str:
    retrieval_dir = Path(__file__).parents[1] / "retrieval"
    digest = hashlib.sha256()
    for name in (
        "budget.py",
        "eligibility.py",
        "index.py",
        "models.py",
        "request.py",
        "service.py",
    ):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((retrieval_dir / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _object(value: Any, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return value


def _object_list(value: JsonObject, field: str) -> list[JsonObject]:
    items = _list(value, field)
    if not all(isinstance(item, dict) for item in items):
        raise TypeError(f"{field} must contain objects")
    return items


def _list(value: Mapping[str, Any], field: str) -> list[Any]:
    items = value.get(field)
    if not isinstance(items, list):
        raise TypeError(f"{field} must be an array")
    return items


def _object_mapping(value: JsonObject, field: str) -> Mapping[str, Any]:
    result = value.get(field)
    if not isinstance(result, dict):
        raise TypeError(f"{field} must be an object")
    return result


def main() -> None:
    root = Path(__file__).parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", default=root / "evaluation/level0/knowledge-fixture.json"
    )
    parser.add_argument(
        "--queries", default=root / "evaluation/level0/golden-queries.json"
    )
    parser.add_argument(
        "--profiles", default=root / "evaluation/level0/budget-profiles.json"
    )
    parser.add_argument("--output", default=root / "evaluation/level0/baseline.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="kasane-level0-eval-") as temp:
        report = generate_level0_report(args.fixture, args.queries, args.profiles, temp)
    write_report(report, args.output)


if __name__ == "__main__":
    main()
