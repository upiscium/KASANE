"""Context Capsule accounting and minimum-budget preflight."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .models import RetrievalRequest

ExactTokenCounter = Callable[[str], int]


def accounting_payload(capsule: Mapping[str, Any]) -> bytes:
    projection = {key: value for key, value in capsule.items() if key != "budget"}
    return json.dumps(
        projection,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True)
class Measurement:
    method: str
    guarantee: str
    used: int
    hard_limit: int
    limit_unit: str
    serialized_bytes: int
    tokenizer_id: str | None

    @property
    def fits(self) -> bool:
        return self.used <= self.hard_limit


class BudgetAccountant:
    def __init__(
        self,
        request: RetrievalRequest,
        token_counters: Mapping[str, ExactTokenCounter],
    ) -> None:
        self.request = request
        self._counter = (
            token_counters.get(request.tokenizer_id)
            if request.tokenizer_id is not None
            else None
        )

    def measure(self, capsule_without_budget: Mapping[str, Any]) -> Measurement:
        payload = accounting_payload(capsule_without_budget)
        if self._counter is not None and self.request.tokenizer_id is not None:
            used = self._counter(payload.decode("utf-8"))
            if isinstance(used, bool) or not isinstance(used, int) or used < 0:
                raise ValueError(
                    "exact token counter must return a non-negative integer"
                )
            return Measurement(
                method="exact_tokenizer",
                guarantee="exact_tokens",
                used=used,
                hard_limit=self.request.budget.max_tokens,
                limit_unit="tokens",
                serialized_bytes=len(payload),
                tokenizer_id=self.request.tokenizer_id,
            )
        return Measurement(
            method="utf8_bytes",
            guarantee="hard_bytes",
            used=len(payload),
            hard_limit=self.request.budget.max_bytes,
            limit_unit="utf8_bytes",
            serialized_bytes=len(payload),
            tokenizer_id=None,
        )

    def finalize(
        self, capsule_without_budget: dict[str, Any], *, outcome: str
    ) -> tuple[dict[str, Any], Measurement]:
        measurement = self.measure(capsule_without_budget)
        capsule = dict(capsule_without_budget)
        report: dict[str, Any] = {
            "requested_tokens": self.request.budget.max_tokens,
            "requested_bytes": self.request.budget.max_bytes,
            "method": measurement.method,
            "guarantee": measurement.guarantee,
            "used": measurement.used,
            "hard_limit": measurement.hard_limit,
            "limit_unit": measurement.limit_unit,
            "serialized_bytes": measurement.serialized_bytes,
            "evidence_items": len(capsule_without_budget["evidence"]),
            "outcome": outcome,
        }
        if measurement.tokenizer_id is not None:
            report["tokenizer_id"] = measurement.tokenizer_id
        capsule["budget"] = report
        return capsule, measurement

    def minimum_failed_payload(
        self, *, level: int = 0, path: tuple[int, ...] = (0,)
    ) -> dict[str, Any]:
        return {
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
                "mode": self.request.mode,
                "level": level,
                "path": list(path),
                "terminal_reason": "budget_limited",
            },
        }

    def budget_too_small_error(self, measurement: Measurement) -> dict[str, Any]:
        accounting: dict[str, Any] = {
            "method": measurement.method,
            "unit": measurement.limit_unit,
            "requested": measurement.hard_limit,
            "minimum_required": measurement.used,
        }
        if measurement.tokenizer_id is not None:
            accounting["tokenizer_id"] = measurement.tokenizer_id
        return {
            "schema_version": "0.1",
            "error": {
                "code": "budget_too_small_for_capsule",
                "message": "The selected hard boundary cannot contain a Context Capsule.",
            },
            "accounting": accounting,
        }
