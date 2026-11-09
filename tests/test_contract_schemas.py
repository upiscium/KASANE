from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).parents[1]
SCHEMA_DIR = ROOT / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


REQUEST_SCHEMA = load_schema("retrieval-request.schema.json")
CAPSULE_SCHEMA = load_schema("context-capsule.schema.json")
ERROR_SCHEMA = load_schema("retrieval-error.schema.json")
REQUEST_VALIDATOR = Draft202012Validator(REQUEST_SCHEMA)
CAPSULE_VALIDATOR = Draft202012Validator(CAPSULE_SCHEMA)
ERROR_VALIDATOR = Draft202012Validator(ERROR_SCHEMA)


def budgeted_projection(capsule: dict[str, Any]) -> bytes:
    projection = {key: value for key, value in capsule.items() if key != "budget"}
    return json.dumps(
        projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def minimum_failed_capsule(retrieval_request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "0.1",
        "status": "failed",
        "context": "",
        "critical_facts": [],
        "constraints": [],
        "pitfalls": [],
        "unresolved": [
            {
                "id": "budget",
                "kind": "insufficient_evidence",
                "question": "insufficient budget for context capsule",
                "knowledge_refs": [],
                "evidence_refs": [],
            }
        ],
        "knowledge_refs": [],
        "evidence": [],
        "retrieval": {
            "mode": retrieval_request["mode"],
            "level": 0,
            "path": [0],
            "terminal_reason": "budget_limited",
        },
        "budget": {
            "requested_tokens": retrieval_request["budget"]["max_tokens"],
            "requested_bytes": retrieval_request["budget"]["max_bytes"],
            "method": "utf8_bytes",
            "guarantee": "hard_bytes",
            "used": 0,
            "hard_limit": retrieval_request["budget"]["max_bytes"],
            "limit_unit": "utf8_bytes",
            "serialized_bytes": 0,
            "evidence_items": 0,
            "outcome": "failed",
        },
    }
    projection = budgeted_projection(result)
    result["budget"]["serialized_bytes"] = len(projection)
    tokenizer = retrieval_request.get("tokenizer")
    if tokenizer and tokenizer["id"] == "test:unicode-codepoint-v1":
        result["budget"].update(
            {
                "method": "exact_tokenizer",
                "guarantee": "exact_tokens",
                "tokenizer_id": tokenizer["id"],
                "used": len(projection.decode("utf-8")),
                "hard_limit": retrieval_request["budget"]["max_tokens"],
                "limit_unit": "tokens",
            }
        )
    else:
        result["budget"]["used"] = len(projection)
    return result


def budget_too_small_error(capsule: dict[str, Any]) -> dict[str, Any]:
    budget = capsule["budget"]
    return {
        "schema_version": "0.1",
        "error": {
            "code": "budget_too_small_for_capsule",
            "message": "The selected hard boundary cannot contain a Context Capsule.",
        },
        "accounting": {
            "method": budget["method"],
            "unit": budget["limit_unit"],
            "requested": budget["hard_limit"],
            "minimum_required": budget["used"],
            **(
                {"tokenizer_id": budget["tokenizer_id"]}
                if "tokenizer_id" in budget
                else {}
            ),
        },
    }


@pytest.fixture
def retrieval_request() -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "query": "How must a pending action be revalidated?",
        "task": {
            "summary": "Implement a safe effect-time check",
            "repository": "VillagerAgent",
            "languages": ["Python"],
            "topics": ["execution integrity"],
        },
        "scope": ["global", "project/villager-agent"],
        "mode": "auto",
        "budget": {"max_tokens": 800, "max_bytes": 3200, "max_evidence_items": 5},
    }


@pytest.fixture
def capsule() -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "0.1",
        "status": "complete",
        "context": "Revalidate the retained exact action at effect time.",
        "critical_facts": [
            {
                "id": "fact-1",
                "statement": "Admission-time validity does not imply effect-time validity.",
                "confidence": "high",
                "knowledge_refs": ["knowledge-1"],
                "evidence_refs": ["evidence-1"],
            }
        ],
        "constraints": [],
        "pitfalls": [],
        "unresolved": [],
        "knowledge_refs": [
            {
                "id": "knowledge-1",
                "uri": "vault://global/execution-integrity/effect-time-authority",
                "revision": "v1",
            }
        ],
        "evidence": [
            {
                "id": "evidence-1",
                "knowledge_ref": "knowledge-1",
                "excerpt": "Authority validation occurs immediately before native effect.",
                "provenance": {
                    "source_type": "repository",
                    "handle": "repo://VillagerAgent/docs/authority.md#effect-time",
                },
            }
        ],
        "retrieval": {
            "mode": "auto",
            "level": 1,
            "path": [0, 1],
            "terminal_reason": "sufficient",
        },
        "budget": {
            "requested_tokens": 800,
            "requested_bytes": 3200,
            "method": "utf8_bytes",
            "guarantee": "hard_bytes",
            "used": 0,
            "hard_limit": 3200,
            "limit_unit": "utf8_bytes",
            "serialized_bytes": 0,
            "evidence_items": 1,
            "outcome": "within_budget",
        },
    }
    measured_bytes = len(budgeted_projection(result))
    result["budget"]["used"] = measured_bytes
    result["budget"]["serialized_bytes"] = measured_bytes
    return result


def validate_contract_pair(
    retrieval_request: dict[str, Any], capsule: dict[str, Any]
) -> None:
    REQUEST_VALIDATOR.validate(retrieval_request)
    CAPSULE_VALIDATOR.validate(capsule)

    assert capsule["retrieval"]["mode"] == retrieval_request["mode"]
    path = capsule["retrieval"]["path"]
    assert path[-1] == capsule["retrieval"]["level"]
    assert path == sorted(path) and len(path) == len(set(path))
    if retrieval_request["mode"] == "fast":
        assert path == [0]
    elif retrieval_request["mode"] == "auto":
        assert path[0] == 0

    budget = capsule["budget"]
    projection = budgeted_projection(capsule)
    assert budget["requested_tokens"] == retrieval_request["budget"]["max_tokens"]
    assert budget["requested_bytes"] == retrieval_request["budget"]["max_bytes"]
    assert budget["used"] <= budget["hard_limit"]
    assert budget["serialized_bytes"] == len(projection)
    if budget["method"] == "exact_tokenizer":
        assert budget["guarantee"] == "exact_tokens"
        assert budget["hard_limit"] == budget["requested_tokens"]
        if budget["tokenizer_id"] == "test:unicode-codepoint-v1":
            assert budget["used"] == len(projection.decode("utf-8"))
    else:
        assert budget["guarantee"] == "hard_bytes"
        assert budget["hard_limit"] == budget["requested_bytes"]
        assert budget["used"] == budget["serialized_bytes"]

    assert budget["evidence_items"] == len(capsule["evidence"])
    assert len(capsule["evidence"]) <= retrieval_request["budget"]["max_evidence_items"]

    knowledge_ids = {item["id"] for item in capsule["knowledge_refs"]}
    evidence_ids = {item["id"] for item in capsule["evidence"]}
    assert len(knowledge_ids) == len(capsule["knowledge_refs"])
    assert len(evidence_ids) == len(capsule["evidence"])
    assert all(item["knowledge_ref"] in knowledge_ids for item in capsule["evidence"])

    claims = capsule["critical_facts"] + capsule["constraints"] + capsule["pitfalls"]
    for item in claims + capsule["unresolved"]:
        assert set(item["knowledge_refs"]) <= knowledge_ids
        assert set(item["evidence_refs"]) <= evidence_ids

    expected_outcome = {
        "complete": "within_budget",
        "degraded": "degraded",
        "failed": "failed",
    }
    assert budget["outcome"] == expected_outcome[capsule["status"]]


def test_schemas_are_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(REQUEST_SCHEMA)
    Draft202012Validator.check_schema(CAPSULE_SCHEMA)
    Draft202012Validator.check_schema(ERROR_SCHEMA)
    assert REQUEST_SCHEMA["$id"] == "urn:kasane:schema:v0.1:retrieval-request"
    assert CAPSULE_SCHEMA["$id"] == "urn:kasane:schema:v0.1:context-capsule"
    assert ERROR_SCHEMA["$id"] == "urn:kasane:schema:v0.1:retrieval-error"


def test_every_schema_id_is_a_kasane_urn_matching_its_file() -> None:
    schema_dir = Path(__file__).resolve().parents[1] / "schemas"
    files = sorted(schema_dir.glob("*.schema.json"))
    assert len(files) == 5
    for path in files:
        schema = json.loads(path.read_text("utf-8"))
        Draft202012Validator.check_schema(schema)
        name = path.name.removesuffix(".schema.json")
        assert schema["$id"] == f"urn:kasane:schema:v0.1:{name}"


def test_simple_query_does_not_require_task() -> None:
    REQUEST_VALIDATOR.validate(
        {
            "schema_version": "0.1",
            "query": "What constraints apply?",
            "mode": "fast",
            "budget": {"max_tokens": 200, "max_bytes": 800, "max_evidence_items": 2},
        }
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"mode": "unbounded"}),
        lambda value: value.update({"scope": ["sqlite:row:42"]}),
        lambda value: value["budget"].update({"max_tokens": 0}),
        lambda value: value["budget"].update({"max_bytes": 0}),
        lambda value: value["budget"].update({"max_evidence_items": -1}),
        lambda value: value.update({"raw_top_k": 100}),
        lambda value: value.pop("budget"),
    ],
)
def test_invalid_retrieval_requests_are_rejected(
    retrieval_request: dict[str, Any], mutation: Any
) -> None:
    mutation(retrieval_request)
    with pytest.raises(ValidationError):
        REQUEST_VALIDATOR.validate(retrieval_request)


def test_valid_request_and_capsule_pair(
    retrieval_request: dict[str, Any], capsule: dict[str, Any]
) -> None:
    validate_contract_pair(retrieval_request, capsule)


def test_raw_chunk_dump_is_not_a_capsule_field(capsule: dict[str, Any]) -> None:
    capsule["raw_chunks"] = ["unbounded source text"]
    with pytest.raises(ValidationError):
        CAPSULE_VALIDATOR.validate(capsule)


def test_storage_identity_cannot_replace_vault_uri(capsule: dict[str, Any]) -> None:
    capsule["knowledge_refs"][0]["uri"] = "sqlite://knowledge/42"
    with pytest.raises(ValidationError):
        CAPSULE_VALIDATOR.validate(capsule)


@pytest.mark.parametrize(
    "uri",
    [
        "vault://global/agent-development/verify-before-pass",
        "vault://project/terreate/buffer-write-contract",
        "vault://research/execution-integrity/admission-effect-gap",
        "vault://private/team-a/review-notes",
    ],
)
def test_nested_vault_identities_are_valid(capsule: dict[str, Any], uri: str) -> None:
    capsule["knowledge_refs"][0]["uri"] = uri
    CAPSULE_VALIDATOR.validate(capsule)


@pytest.mark.parametrize(
    "scope",
    [
        ["global/agent-development"],
        ["project/terreate"],
        ["research/execution-integrity"],
        ["private/team-a"],
    ],
)
def test_scope_uses_the_namespace_grammar(
    retrieval_request: dict[str, Any], scope: list[str]
) -> None:
    retrieval_request["scope"] = scope
    REQUEST_VALIDATOR.validate(retrieval_request)


def test_claim_requires_traceability(capsule: dict[str, Any]) -> None:
    claim = capsule["critical_facts"][0]
    claim["knowledge_refs"] = []
    claim["evidence_refs"] = []
    with pytest.raises(ValidationError):
        CAPSULE_VALIDATOR.validate(capsule)


def test_conflict_requires_two_evidence_handles(capsule: dict[str, Any]) -> None:
    capsule["unresolved"] = [
        {
            "id": "question-1",
            "kind": "conflicting_evidence",
            "question": "Which constraint is current?",
            "knowledge_refs": ["knowledge-1"],
            "evidence_refs": ["evidence-1"],
        }
    ]
    with pytest.raises(ValidationError):
        CAPSULE_VALIDATOR.validate(capsule)


@pytest.mark.parametrize(
    ("field", "value"),
    [("used", 3201), ("hard_limit", 3199), ("evidence_items", 2)],
)
def test_cross_contract_budget_mismatch_is_rejected(
    retrieval_request: dict[str, Any],
    capsule: dict[str, Any],
    field: str,
    value: int,
) -> None:
    capsule["budget"][field] = value
    with pytest.raises(AssertionError):
        validate_contract_pair(retrieval_request, capsule)


def test_fast_mode_cannot_escalate(
    retrieval_request: dict[str, Any], capsule: dict[str, Any]
) -> None:
    retrieval_request["mode"] = "fast"
    capsule["retrieval"]["mode"] = "fast"
    with pytest.raises(ValidationError):
        validate_contract_pair(retrieval_request, capsule)


def test_auto_mode_cannot_skip_level_one(capsule: dict[str, Any]) -> None:
    capsule["retrieval"]["level"] = 2
    capsule["retrieval"]["path"] = [0, 2]
    with pytest.raises(ValidationError):
        CAPSULE_VALIDATOR.validate(capsule)


def test_fallback_usage_is_computed_from_budgeted_content(
    retrieval_request: dict[str, Any], capsule: dict[str, Any]
) -> None:
    capsule["context"] = "x" * 4000
    with pytest.raises(AssertionError):
        validate_contract_pair(retrieval_request, capsule)


def test_large_caller_visible_metadata_counts_toward_hard_byte_boundary(
    retrieval_request: dict[str, Any], capsule: dict[str, Any]
) -> None:
    capsule["evidence"][0]["provenance"]["handle"] = "p" * 2048
    retrieval_request["budget"]["max_bytes"] = 2500
    capsule["budget"]["requested_bytes"] = 2500
    capsule["budget"]["hard_limit"] = 2500
    CAPSULE_VALIDATOR.validate(capsule)
    assert len(budgeted_projection(capsule)) > retrieval_request["budget"]["max_bytes"]
    with pytest.raises(AssertionError):
        validate_contract_pair(retrieval_request, capsule)


def test_byte_fallback_makes_no_exact_token_guarantee(
    retrieval_request: dict[str, Any], capsule: dict[str, Any]
) -> None:
    validate_contract_pair(retrieval_request, capsule)
    assert capsule["budget"]["guarantee"] == "hard_bytes"
    assert "tokenizer_id" not in capsule["budget"]


def test_exact_test_tokenizer_usage_is_reproduced(
    retrieval_request: dict[str, Any], capsule: dict[str, Any]
) -> None:
    projection = budgeted_projection(capsule)
    capsule["budget"].update(
        {
            "method": "exact_tokenizer",
            "guarantee": "exact_tokens",
            "tokenizer_id": "test:unicode-codepoint-v1",
            "used": len(projection.decode("utf-8")),
            "hard_limit": retrieval_request["budget"]["max_tokens"],
            "limit_unit": "tokens",
            "serialized_bytes": len(projection),
        }
    )
    validate_contract_pair(retrieval_request, capsule)


def test_minimum_failed_capsule_may_sit_exactly_on_byte_boundary(
    retrieval_request: dict[str, Any],
) -> None:
    candidate = minimum_failed_capsule(retrieval_request)
    minimum = candidate["budget"]["used"]
    retrieval_request["budget"]["max_bytes"] = minimum
    candidate = minimum_failed_capsule(retrieval_request)
    validate_contract_pair(retrieval_request, candidate)
    assert candidate["budget"]["used"] == candidate["budget"]["hard_limit"]


def test_too_small_byte_budget_returns_protocol_error_not_capsule(
    retrieval_request: dict[str, Any],
) -> None:
    candidate = minimum_failed_capsule(retrieval_request)
    retrieval_request["budget"]["max_bytes"] = candidate["budget"]["used"] - 1
    impossible = minimum_failed_capsule(retrieval_request)
    with pytest.raises(AssertionError):
        validate_contract_pair(retrieval_request, impossible)
    error = budget_too_small_error(impossible)
    ERROR_VALIDATOR.validate(error)
    assert error["error"]["code"] == "budget_too_small_for_capsule"


def test_too_small_exact_token_budget_returns_protocol_error(
    retrieval_request: dict[str, Any],
) -> None:
    retrieval_request["tokenizer"] = {"id": "test:unicode-codepoint-v1"}
    candidate = minimum_failed_capsule(retrieval_request)
    retrieval_request["budget"]["max_tokens"] = candidate["budget"]["used"] - 1
    impossible = minimum_failed_capsule(retrieval_request)
    with pytest.raises(AssertionError):
        validate_contract_pair(retrieval_request, impossible)
    ERROR_VALIDATOR.validate(budget_too_small_error(impossible))


def test_failed_capsule_has_no_synthesized_payload(capsule: dict[str, Any]) -> None:
    failed = copy.deepcopy(capsule)
    failed.update(
        {
            "status": "failed",
            "context": "",
            "critical_facts": [],
            "constraints": [],
            "pitfalls": [],
            "evidence": [],
            "unresolved": [
                {
                    "id": "failure-1",
                    "kind": "insufficient_evidence",
                    "question": "No truthful capsule fits the hard budget.",
                    "knowledge_refs": [],
                    "evidence_refs": [],
                }
            ],
        }
    )
    failed["budget"]["evidence_items"] = 0
    failed["budget"]["outcome"] = "failed"
    CAPSULE_VALIDATOR.validate(failed)


def test_schema_files_are_the_documented_ssot() -> None:
    for document in ("architecture.md", "retrieval-contract.md", "context-capsule.md"):
        text = (ROOT / "docs" / document).read_text(encoding="utf-8")
        assert "SSOT" in text or "single source of truth" in text
