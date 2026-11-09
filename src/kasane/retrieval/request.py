"""Strict parsing for the existing Retrieval Request v0.1 wire shape."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .models import RetrievalBudget, RetrievalRequest, RetrievalTask

_SCOPE_PATTERN = re.compile(
    r"^(global(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*|"
    r"(?:project|research|private)/[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*)$"
)


def parse_retrieval_request(value: Mapping[str, Any]) -> RetrievalRequest:
    """Parse the schema-defined request without extending its public fields."""

    _require_keys(value, {"schema_version", "query", "mode", "budget"})
    _reject_extra(
        value,
        {"schema_version", "query", "task", "scope", "mode", "tokenizer", "budget"},
    )
    if value["schema_version"] != "0.1":
        raise ValueError("schema_version must be '0.1'")
    query = _bounded_string(value["query"], "query", 1, 4096)
    mode = value["mode"]
    if not isinstance(mode, str) or mode not in {"fast", "auto", "thorough"}:
        raise ValueError("mode is not supported by Retrieval Request v0.1")

    scope_value = value.get("scope", ["global"])
    if not isinstance(scope_value, list) or not 1 <= len(scope_value) <= 32:
        raise ValueError("scope must contain between 1 and 32 selectors")
    scope: list[str] = []
    for selector in scope_value:
        if not isinstance(selector, str) or not _SCOPE_PATTERN.fullmatch(selector):
            raise ValueError("scope selector does not match the v0.1 grammar")
        scope.append(selector)
    if len(set(scope)) != len(scope):
        raise ValueError("scope selectors must be unique")

    budget_value = _mapping(value["budget"], "budget")
    _require_keys(budget_value, {"max_tokens", "max_bytes", "max_evidence_items"})
    _reject_extra(budget_value, {"max_tokens", "max_bytes", "max_evidence_items"})
    budget = RetrievalBudget(
        max_tokens=_bounded_integer(
            budget_value["max_tokens"], "budget.max_tokens", 1, 1_000_000
        ),
        max_bytes=_bounded_integer(
            budget_value["max_bytes"], "budget.max_bytes", 1, 1_000_000_000
        ),
        max_evidence_items=_bounded_integer(
            budget_value["max_evidence_items"],
            "budget.max_evidence_items",
            0,
            1000,
        ),
    )

    tokenizer_id: str | None = None
    if "tokenizer" in value:
        tokenizer = _mapping(value["tokenizer"], "tokenizer")
        _require_keys(tokenizer, {"id"})
        _reject_extra(tokenizer, {"id"})
        tokenizer_id = _bounded_string(tokenizer["id"], "tokenizer.id", 1, 256)

    task = _parse_task(value["task"]) if "task" in value else None
    return RetrievalRequest(
        query=query,
        scope=tuple(scope),
        mode=mode,
        budget=budget,
        tokenizer_id=tokenizer_id,
        task=task,
    )


def _parse_task(value: Any) -> RetrievalTask:
    task = _mapping(value, "task")
    _reject_extra(task, {"summary", "repository", "languages", "topics"})
    if not task:
        raise ValueError("task must contain at least one field")
    summary = (
        _bounded_string(task["summary"], "task.summary", 1, 2048)
        if "summary" in task
        else None
    )
    repository = (
        _bounded_string(task["repository"], "task.repository", 1, 512)
        if "repository" in task
        else None
    )
    languages = _string_array(task.get("languages", []), "task.languages", 20, 64)
    topics = _string_array(task.get("topics", []), "task.topics", 50, 128)
    return RetrievalTask(summary, repository, languages, topics)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    return value


def _require_keys(value: Mapping[str, Any], required: set[str]) -> None:
    missing = required - set(value)
    if missing:
        raise ValueError(f"missing required fields: {sorted(missing)}")


def _reject_extra(value: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"unexpected fields: {sorted(extra)}")


def _bounded_string(value: Any, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} must be a string of length {minimum}..{maximum}")
    return value


def _bounded_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _string_array(
    value: Any, field: str, max_items: int, max_length: int
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{field} must be an array with at most {max_items} items")
    result = tuple(_bounded_string(item, field, 1, max_length) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} items must be unique")
    return result
