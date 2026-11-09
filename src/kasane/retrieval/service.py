"""Deterministic Level 0 retrieval and Context Capsule assembly."""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kasane.trust.policy import TrustPolicyConfig
from kasane.trust.policy import evaluate as evaluate_trust
from kasane.vault.models import KnowledgeRecord, KnowledgeStatus
from kasane.vault.repository import VaultRepository

from .budget import BudgetAccountant, ExactTokenCounter
from .eligibility import (
    RetrievalEligibility,
    has_unevaluated_conditions,
    record_freshness,
    scope_specificity,
    status_tier,
)
from .index import DerivedLexicalIndex, normalized_terms
from .models import (
    RankedKnowledge,
    RetrievalDiagnostics,
    RetrievalRequest,
    RetrievalResult,
)
from .request import parse_retrieval_request
from .rerank import (
    MAX_PROVIDER_DOCUMENT_BYTES,
    MAX_RERANK_CANDIDATES,
    RerankCandidate,
    RerankedCandidate,
    bounded_document,
    union_candidates,
)
from .semantic_candidates import MAX_SEMANTIC_CANDIDATES, SemanticCandidateService
from .semantic_index import DerivedSemanticIndex

Clock = Callable[[], datetime]
Monotonic = Callable[[], float]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Level0RetrievalService:
    """Deterministic, trust-labeled retrieval with a disposable lexical index.

    Each request selects a minimum trust tier (default T1); every returned
    knowledge ref carries its trust label (#41).
    """

    def __init__(
        self,
        repository: VaultRepository,
        index_path: str | Path | None = None,
        *,
        clock: Clock = utc_now,
        token_counters: Mapping[str, ExactTokenCounter] | None = None,
        monotonic: Monotonic = time.perf_counter,
        trust_policy: TrustPolicyConfig | None = None,
    ) -> None:
        canonical_path = repository.database_path
        derived_path = Path(
            index_path or canonical_path.with_suffix(".level0-index.db")
        )
        if derived_path.resolve() == canonical_path.resolve():
            raise ValueError("derived lexical index must not be the canonical Vault DB")
        self.repository = repository
        self.index = DerivedLexicalIndex(derived_path)
        self._clock = clock
        self._eligibility = RetrievalEligibility()
        self._token_counters = dict(token_counters or {})
        self._monotonic = monotonic
        self._trust_policy = trust_policy

    def rebuild_index(self) -> str:
        """Explicitly rebuild disposable lexical state from canonical records."""

        return self.index.rebuild(self.repository.list_knowledge()).watermark

    def retrieve(self, request_value: Mapping[str, Any]) -> RetrievalResult:
        started = self._monotonic()
        request = parse_retrieval_request(request_value)
        accountant = BudgetAccountant(request, self._token_counters)
        minimum_payload = accountant.minimum_failed_payload()
        minimum_measurement = accountant.measure(minimum_payload)
        if not minimum_measurement.fits:
            return self._result(
                capsule=None,
                error=accountant.budget_too_small_error(minimum_measurement),
                counts=(0, 0, 0, 0, 0, 0, 0),
                index_rebuilt=False,
                watermark="not-synchronized",
                serialized_bytes=0,
                started=started,
            )

        records = self.repository.list_knowledge()
        sync = self.index.synchronize(records)
        eligible, counts = self._eligible_records(records, request)
        try:
            search_hits = self.index.search(request.query)
        except sqlite3.DatabaseError:
            sync = self.index.rebuild(records)
            search_hits = self.index.search(request.query)
        hits = {hit.knowledge_ref: hit for hit in search_hits}
        ranked = self._rank(eligible, hits, request)
        capsule, measurement, selected_count = self._assemble(
            ranked, records, request, accountant
        )
        return self._result(
            capsule=capsule,
            error=None,
            counts=(counts[0], selected_count, *counts[1:]),
            index_rebuilt=sync.rebuilt,
            watermark=sync.watermark,
            serialized_bytes=measurement.serialized_bytes,
            started=started,
        )

    def trust_label(self, record: KnowledgeRecord) -> dict[str, Any]:
        """Trust label for a live record, derived from the event log (#40)."""

        assessment = evaluate_trust(
            record.status,
            record.verification_outcome,
            self.repository.list_events(record.knowledge_ref),
            self._trust_policy,
        )
        tier = assessment.tier
        if tier is None:  # pragma: no cover - retired records are never eligible
            raise ValueError(f"{record.knowledge_ref} has no trust tier")
        # Compact on purpose: labels are paid for in every capsule's budget.
        label: dict[str, Any] = {"tier": f"T{int(tier)}"}
        if assessment.revalidation_requested:
            label["revalidation"] = True
        return label

    def _tier(self, record: KnowledgeRecord) -> int:
        if record.status is not KnowledgeStatus.VERIFIED:
            return status_tier(record) or 0
        tier = evaluate_trust(
            record.status,
            record.verification_outcome,
            self.repository.list_events(record.knowledge_ref),
            self._trust_policy,
        ).tier
        return int(tier) if tier is not None else 0

    def _eligible_records(
        self, records: list[KnowledgeRecord], request: RetrievalRequest
    ) -> tuple[list[KnowledgeRecord], tuple[int, int, int, int, int, int]]:
        eligible, counts = self._eligibility.filter(
            records, request.scope, self._clock(), request.min_trust, self._tier
        )
        return eligible, (
            counts.candidate_count,
            counts.excluded_scope,
            counts.excluded_lifecycle,
            counts.excluded_applicability,
            counts.excluded_stale,
            counts.malformed_freshness,
        )

    def _rank(
        self,
        eligible: list[KnowledgeRecord],
        hits: Mapping[str, Any],
        request: RetrievalRequest,
    ) -> list[RankedKnowledge]:
        query = normalized_terms(request.query)
        requested_topics = (
            {normalized_terms(topic) for topic in request.task.topics}
            if request.task is not None
            else set()
        )
        ranked: list[RankedKnowledge] = []
        for record in eligible:
            hit = hits.get(record.knowledge_ref)
            if hit is None:
                continue
            tags = {normalized_terms(tag) for tag in record.tags}
            path_topics = {
                normalized_terms(part) for part in record.knowledge_path.split("/")
            }
            ranked.append(
                RankedKnowledge(
                    knowledge_ref=record.knowledge_ref,
                    lexical_score=hit.score,
                    exact_title=query == normalized_terms(record.title),
                    exact_tag=query in tags,
                    exact_topic=bool(requested_topics & (tags | path_topics)),
                    scope_specificity=self._scope_specificity(
                        record.knowledge_ref, request.scope
                    ),
                    trust_rank=self._tier(record),
                )
            )
        return sorted(
            ranked,
            key=lambda item: (
                -item.exact_title,
                -item.exact_tag,
                -item.exact_topic,
                item.lexical_score,
                -item.trust_rank,  # tie-breaker only: relevance comes first
                -item.scope_specificity,
                item.knowledge_ref,
            ),
        )

    def _assemble(
        self,
        ranked: list[RankedKnowledge],
        records: list[KnowledgeRecord],
        request: RetrievalRequest,
        accountant: BudgetAccountant,
        *,
        level: int = 0,
        path: tuple[int, ...] = (0,),
    ) -> tuple[dict[str, Any], Any, int]:
        by_ref = {record.knowledge_ref: record for record in records}
        cap = request.budget.max_evidence_items
        candidates = [by_ref[item.knowledge_ref] for item in ranked[:cap]]
        omitted_by_cap = len(ranked) > len(candidates)

        if not ranked:
            payload = self._failed_payload(
                request,
                question="no eligible Level 0 evidence matched the request",
                terminal_reason="insufficient_evidence",
                level=level,
                path=path,
            )
            capsule, measurement = accountant.finalize(payload, outcome="failed")
            if measurement.fits:
                return capsule, measurement, 0
            minimum = accountant.minimum_failed_payload(level=level, path=path)
            capsule, measurement = accountant.finalize(minimum, outcome="failed")
            return capsule, measurement, 0

        if not candidates:
            payload = self._failed_payload(
                request,
                question=(
                    "matching evidence could not be included because "
                    "max_evidence_items is zero"
                ),
                terminal_reason="budget_limited",
                level=level,
                path=path,
            )
            capsule, measurement = accountant.finalize(payload, outcome="failed")
            if measurement.fits:
                return capsule, measurement, 0
            minimum = accountant.minimum_failed_payload(level=level, path=path)
            capsule, measurement = accountant.finalize(minimum, outcome="failed")
            return capsule, measurement, 0

        for selected_count in range(len(candidates), 0, -1):
            selected = candidates[:selected_count]
            degraded = omitted_by_cap or selected_count < len(candidates)
            payload = self._evidence_payload(
                request, selected, degraded=degraded, level=level, path=path
            )
            outcome = "degraded" if degraded else "within_budget"
            capsule, measurement = accountant.finalize(payload, outcome=outcome)
            if measurement.fits:
                return capsule, measurement, selected_count

        minimum = accountant.minimum_failed_payload(level=level, path=path)
        capsule, measurement = accountant.finalize(minimum, outcome="failed")
        return capsule, measurement, 0

    def _assemble_level(
        self,
        ranked: list[Any],
        records: list[KnowledgeRecord],
        request: RetrievalRequest,
        accountant: BudgetAccountant,
        *,
        level: int,
        path: tuple[int, ...],
    ):
        return self._assemble(
            ranked, records, request, accountant, level=level, path=path
        )

    def _evidence_payload(
        self,
        request: RetrievalRequest,
        records: list[KnowledgeRecord],
        *,
        degraded: bool,
        level: int = 0,
        path: tuple[int, ...] = (0,),
    ) -> dict[str, Any]:
        knowledge_refs: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        context_parts: list[str] = []
        for position, record in enumerate(records, start=1):
            knowledge_id = f"knowledge-{position}"
            evidence_id = f"evidence-{position}"
            label = self.trust_label(record)
            knowledge_refs.append(
                {
                    "id": knowledge_id,
                    "uri": record.knowledge_ref,
                    "revision": str(record.revision),
                    "trust": label,
                }
            )
            evidence.append(
                {
                    "id": evidence_id,
                    "knowledge_ref": knowledge_id,
                    "excerpt": self._excerpt(record.body, record.title, request.query),
                    "provenance": self._provenance(record),
                }
            )
            context_parts.append(f"{record.title} [{knowledge_id}, {label['tier']}]")

        unresolved: list[dict[str, Any]] = []
        if degraded:
            unresolved.append(
                {
                    "id": "budget",
                    "kind": "insufficient_evidence",
                    "question": "context budget or evidence cap prevented inclusion of lower-ranked evidence",
                    "knowledge_refs": [],
                    "evidence_refs": [],
                }
            )
        return {
            "schema_version": "0.1",
            "status": "degraded" if degraded else "complete",
            "context": "Relevant knowledge: " + "; ".join(context_parts),
            "critical_facts": [],
            "constraints": [],
            "pitfalls": [],
            "unresolved": unresolved,
            "knowledge_refs": knowledge_refs,
            "evidence": evidence,
            "retrieval": {
                "mode": request.mode,
                "level": level,
                "path": list(path),
                "terminal_reason": "budget_limited" if degraded else "sufficient",
            },
        }

    @staticmethod
    def _failed_payload(
        request: RetrievalRequest,
        *,
        question: str,
        terminal_reason: str,
        level: int = 0,
        path: tuple[int, ...] = (0,),
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
                    "id": "evidence",
                    "kind": "insufficient_evidence",
                    "question": question,
                    "knowledge_refs": [],
                    "evidence_refs": [],
                }
            ],
            "knowledge_refs": [],
            "evidence": [],
            "retrieval": {
                "mode": request.mode,
                "level": level,
                "path": list(path),
                "terminal_reason": terminal_reason,
            },
        }

    def _result(
        self,
        *,
        capsule: dict[str, Any] | None,
        error: dict[str, Any] | None,
        counts: tuple[int, int, int, int, int, int, int],
        index_rebuilt: bool,
        watermark: str,
        serialized_bytes: int,
        started: float,
        level: int = 0,
        path: tuple[int, ...] = (0,),
        lexical_count: int = 0,
        semantic_count: int = 0,
        reranker: Any = None,
        fallback_reason: str | None = None,
        semantic_records_seen: int = 0,
        semantic_records_truncated: int = 0,
        semantic_record_cap: int | None = None,
    ) -> RetrievalResult:
        finished = self._monotonic()
        diagnostics = RetrievalDiagnostics(
            candidate_count=counts[0],
            selected_count=counts[1],
            excluded_scope_count=counts[2],
            excluded_lifecycle_count=counts[3],
            excluded_applicability_count=counts[4],
            excluded_stale_count=counts[5],
            malformed_freshness_count=counts[6],
            index_rebuilt=index_rebuilt,
            index_watermark=watermark,
            serialized_bytes=serialized_bytes,
            elapsed_ms=max(0.0, (finished - started) * 1000),
            level=level,
            path=path,
            lexical_candidate_count=lexical_count,
            semantic_candidate_count=semantic_count,
            reranker_provider_id=getattr(reranker, "provider_id", None),
            reranker_model_id=getattr(reranker, "model_id", None),
            fallback_reason=fallback_reason,
            semantic_records_seen=semantic_records_seen,
            semantic_records_truncated=semantic_records_truncated,
            semantic_record_cap=semantic_record_cap,
        )
        return RetrievalResult(capsule, error, diagnostics)

    @staticmethod
    def _scope_specificity(knowledge_ref: str, selectors: tuple[str, ...]) -> int:
        return scope_specificity(knowledge_ref, selectors)

    @staticmethod
    def _freshness(stale_after: str | None, now: datetime) -> str:
        return record_freshness(stale_after, now)

    @staticmethod
    def _has_unevaluated_conditions(record: KnowledgeRecord) -> bool:
        return has_unevaluated_conditions(record)

    @staticmethod
    def _excerpt(body: str, title: str, query: str, limit: int = 320) -> str:
        source = body.strip() or title.strip()
        if not source:
            return "Untitled canonical knowledge"
        tokens = normalized_terms(query).split()
        folded = source.casefold()
        positions = [folded.find(token) for token in tokens if folded.find(token) >= 0]
        center = min(positions) if positions else 0
        start = max(0, center - limit // 3)
        end = min(len(source), start + limit)
        start = max(0, end - limit)
        excerpt = source[start:end]
        if start > 0:
            excerpt = "…" + excerpt[1:]
        if end < len(source):
            excerpt = excerpt[:-1] + "…"
        return excerpt

    @staticmethod
    def _provenance(record: KnowledgeRecord) -> dict[str, str]:
        allowed = {"okf", "repository", "web", "operator", "other"}
        candidates: list[Any] = []
        if isinstance(record.sources, list):
            candidates.extend(record.sources)
        if isinstance(record.provenance, dict):
            candidates.append(record.provenance)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            source_type = candidate.get("source_type", candidate.get("kind", "other"))
            handle = candidate.get("handle", candidate.get("uri"))
            if isinstance(handle, str) and 1 <= len(handle) <= 2048:
                return {
                    "source_type": (
                        source_type
                        if isinstance(source_type, str) and source_type in allowed
                        else "other"
                    ),
                    "handle": handle,
                }
        return {"source_type": "other", "handle": record.knowledge_ref}


class Level1RetrievalService:
    """Hybrid Level 1 retrieval; Level 0 remains the safe fallback boundary."""

    def __init__(
        self,
        level0: Level0RetrievalService | VaultRepository,
        semantic_service: Any | None = None,
        reranker: Any | None = None,
        *,
        semantic_index_path: str | Path | None = None,
        embedding_provider: Any | None = None,
        rerank_provider: Any | None = None,
    ) -> None:
        self.level0 = (
            Level0RetrievalService(level0)
            if isinstance(level0, VaultRepository)
            else level0
        )
        if semantic_service is None and embedding_provider is not None:
            canonical_path = self.level0.repository.database_path
            path = semantic_index_path or canonical_path.with_suffix(
                ".level1-semantic.db"
            )
            if semantic_index_path is not None:
                self._validate_semantic_index_path(path, canonical_path)
            semantic_service = SemanticCandidateService(
                DerivedSemanticIndex(
                    path,
                    embedding_provider,
                    canonical_database_path=self.level0.repository.database_path,
                )
            )
        self.semantic_service = semantic_service
        self.reranker = rerank_provider or reranker

    @staticmethod
    def _validate_semantic_index_path(
        path: str | Path, canonical_path: str | Path
    ) -> None:
        """Keep caller-selected derived state inside the Vault's app directory."""
        candidate = Path(path).expanduser()
        root = Path(canonical_path).expanduser().resolve(strict=False).parent
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "semantic index path must be inside the canonical Vault database directory"
            ) from exc
        # Match DerivedSemanticIndex's final-component protection.  Resolving
        # above also prevents an existing parent symlink from escaping root.
        if candidate.is_symlink():
            raise ValueError("semantic index database path must not be a symlink")

    def retrieve(self, request_value: Mapping[str, Any]) -> RetrievalResult:
        request = parse_retrieval_request(request_value)
        if request.mode == "fast":
            return self.level0.retrieve(request_value)
        started = self.level0._monotonic()
        level0_result: RetrievalResult | None = None
        if request.mode == "auto":
            # Auto is deliberately conservative: the cheap, canonical path is
            # authoritative whenever it produced an unambiguous answer.
            level0_result = self.level0.retrieve(request_value)
            if not self._needs_escalation(level0_result, request):
                return self._retime(level0_result, started)
        # Level 1 has no caller-facing default providers.  An application that
        # has not explicitly configured both boundaries gets the authoritative
        # Level 0 result, rather than silently using a test implementation.
        if self.semantic_service is None or self.reranker is None:
            return self._retime(
                level0_result or self.level0.retrieve(request_value),
                started,
                fallback_reason="level1_provider_unavailable",
            )
        # Thorough may preflight directly; auto reaches this point only after
        # the deterministic Level 0 path has decided escalation is warranted.
        # Keep this before any semantic index synchronization or provider call.
        accountant = BudgetAccountant(request, self.level0._token_counters)
        minimum_measurement = accountant.measure(
            accountant.minimum_failed_payload(level=1, path=(0, 1))
        )
        if not minimum_measurement.fits:
            # Auto has already obtained an authoritative Level 0 answer.  A
            # budget that can hold that answer but cannot hold the Level 1
            # protocol envelope must not turn escalation into an error (or
            # invoke either provider).
            if request.mode == "auto" and level0_result is not None:
                return self._retime(level0_result, started)
            return self.level0._result(
                capsule=None,
                error=accountant.budget_too_small_error(minimum_measurement),
                counts=(0, 0, 0, 0, 0, 0, 0),
                index_rebuilt=False,
                watermark="not-synchronized",
                serialized_bytes=0,
                started=started,
                level=1,
                path=(0, 1),
            )
        try:
            records = self.level0.repository.list_knowledge()
            eligible, counts = self.level0._eligible_records(records, request)
            self.level0.index.synchronize(records)
            lexical = [
                hit
                for hit in self.level0.index.search(request.query)
                if hit.knowledge_ref in {r.knowledge_ref for r in eligible}
            ][:MAX_RERANK_CANDIDATES]
            semantic_result = self.semantic_service.generate(
                records,
                request.query,
                request.scope,
                self.level0._clock(),
                min_trust=request.min_trust,
                tier_of=self.level0._tier,
            )
            eligible_refs = {r.knowledge_ref for r in eligible}
            semantic_candidates = self._validated_semantic_candidates(
                semantic_result.candidates, eligible_refs
            )
            semantic_by_ref = {hit.knowledge_ref: hit for hit in semantic_candidates}
            by_ref = {record.knowledge_ref: record for record in eligible}
            lexical_candidates = [
                RerankCandidate(
                    hit.knowledge_ref,
                    bounded_document(
                        by_ref[hit.knowledge_ref].title,
                        by_ref[hit.knowledge_ref].body,
                        MAX_PROVIDER_DOCUMENT_BYTES,
                    ),
                    hit.score,
                    None,
                )
                for hit in lexical
                if hit.knowledge_ref in by_ref
            ]
            semantic_candidates_for_rerank = [
                RerankCandidate(
                    hit.knowledge_ref,
                    bounded_document(
                        by_ref[hit.knowledge_ref].title,
                        by_ref[hit.knowledge_ref].body,
                        MAX_PROVIDER_DOCUMENT_BYTES,
                    ),
                    None,
                    hit.semantic_score,
                )
                for hit in semantic_candidates
            ]
            canonical_winner = self._canonical_winner_ref(level0_result)
            candidates = self._mixed_candidates(
                lexical_candidates,
                semantic_candidates_for_rerank,
                canonical_winner,
                by_ref,
            )
            refs = tuple(item.knowledge_ref for item in candidates)
            ordered = self.reranker.rerank(request.query, candidates)
            ordered_refs = self._validated_rerank_output(ordered, set(refs))
            # Omission is safe and deterministic; unknown or malformed output
            # is not, and is handled by the provider-failure fallback below.
            ordered_refs.extend(ref for ref in refs if ref not in ordered_refs)
            if request.mode == "auto" and level0_result is not None:
                # Preserve the strongest lexical candidate before applying the
                # optional semantic ordering.  This is deliberately based on
                # ranked evidence, not query IDs or fixture labels: strong
                # lexical evidence cannot be displaced by a reranker, while new
                # semantic candidates remain available for recovery.
                level0_refs = [canonical_winner] if canonical_winner is not None else []
                visible = set(ordered_refs[: request.budget.max_evidence_items])
                if level0_refs and level0_refs[0] not in visible:
                    ordered_refs = [
                        level0_refs[0],
                        *(ref for ref in ordered_refs if ref != level0_refs[0]),
                    ]
            ranked = [by_ref[ref] for ref in ordered_refs]
            accountant = BudgetAccountant(request, self.level0._token_counters)
            capsule, measurement, selected = self.level0._assemble_level(
                ranked, records, request, accountant, level=1, path=(0, 1)
            )
            return self.level0._result(
                capsule=capsule,
                error=None,
                counts=(len(refs), selected, *counts[1:]),
                index_rebuilt=semantic_result.synchronization.rebuilt,
                watermark=semantic_result.synchronization.watermark,
                serialized_bytes=measurement.serialized_bytes,
                started=started,
                level=1,
                path=(0, 1),
                lexical_count=len(lexical),
                semantic_count=len(semantic_by_ref),
                reranker=self.reranker,
                semantic_records_seen=getattr(semantic_result, "records_seen", 0),
                semantic_records_truncated=getattr(
                    semantic_result, "records_truncated", 0
                ),
                semantic_record_cap=getattr(semantic_result, "record_cap", None),
            )
        except sqlite3.DatabaseError:
            reason = "level1_index_failure"
        except (RuntimeError, TimeoutError, ConnectionError):
            reason = "level1_provider_failure"
        except (TypeError, ValueError):
            # Provider contract and output validation failures are attributable
            # to the provider boundary, not to the canonical retrieval path.
            reason = "level1_provider_failure"
        except Exception:  # noqa: BLE001 - internal failures fall back safely
            reason = "level1_internal_failure"
        else:
            return self._retime(
                level0_result or self.level0.retrieve(request_value), started
            )
        # Provider/index/internal failures are explicitly truthful: no Level 1
        # artifact is emitted, and the canonical Level 0 result is returned.
        return self._retime(
            level0_result or self.level0.retrieve(request_value),
            started,
            fallback_reason=reason,
        )

    @staticmethod
    def _needs_escalation(result: RetrievalResult, request: Any) -> bool:
        if result.error is not None:
            return False
        artifact = result.artifact
        if artifact.get("status") != "complete":
            return True
        # candidate_count includes all eligible records for diagnostics.  Auto
        # escalation must instead inspect the evidence actually returned by
        # Level 0; otherwise every sufficiently populated Vault escalates even
        # for a complete, small lexical match.
        return (
            len(artifact.get("knowledge_refs", [])) > request.budget.max_evidence_items
        )

    @staticmethod
    def _canonical_winner_ref(result: RetrievalResult | None) -> str | None:
        """Return the reference actually selected by Level 0, when available."""
        if result is None or result.capsule is None:
            return None
        refs = result.capsule.get("knowledge_refs")
        if not isinstance(refs, list) or not refs:
            return None
        first = refs[0]
        if not isinstance(first, dict):
            return None
        value = first.get("uri")
        return value if isinstance(value, str) else None

    @staticmethod
    def _mixed_candidates(
        lexical: list[RerankCandidate],
        semantic: list[RerankCandidate],
        canonical_winner: str | None,
        by_ref: dict[str, KnowledgeRecord],
    ) -> tuple[RerankCandidate, ...]:
        """Bound the union while reserving deterministic room for semantic-only hits."""
        merged = list(union_candidates(lexical, semantic))
        merged_refs = {item.knowledge_ref for item in merged}
        if (
            canonical_winner is not None
            and canonical_winner in by_ref
            and canonical_winner not in merged_refs
        ):
            record = by_ref[canonical_winner]
            merged.insert(
                0,
                RerankCandidate(
                    canonical_winner,
                    bounded_document(
                        record.title, record.body, MAX_PROVIDER_DOCUMENT_BYTES
                    ),
                ),
            )

        if len(merged) <= MAX_RERANK_CANDIDATES:
            return tuple(merged)
        # Select each protected opportunity explicitly rather than relying on
        # insertion order: a late lexical Level 0 winner must survive semantic
        # reserve pressure as well as the ordinary lexical/semantic union.
        protected: list[RerankCandidate] = []
        if canonical_winner is not None:
            protected = [
                item for item in merged if item.knowledge_ref == canonical_winner
            ]
        lexical_refs = {item.knowledge_ref for item in lexical}
        protected_refs = {item.knowledge_ref for item in protected}
        semantic_only = [
            item
            for item in semantic
            if item.knowledge_ref not in lexical_refs
            and item.knowledge_ref not in protected_refs
        ]
        reserve = min(
            len(semantic_only),
            max(0, MAX_RERANK_CANDIDATES // 4 - len(protected)),
            MAX_RERANK_CANDIDATES - len(protected),
        )
        selected = [*protected, *semantic_only[:reserve]]
        selected_refs = {item.knowledge_ref for item in selected}
        for item in merged:
            if len(selected) >= MAX_RERANK_CANDIDATES:
                break
            if item.knowledge_ref not in selected_refs:
                selected.append(item)
                selected_refs.add(item.knowledge_ref)
        return tuple(selected[:MAX_RERANK_CANDIDATES])

    @staticmethod
    def _validated_semantic_candidates(
        candidates: Any, eligible_refs: set[str]
    ) -> tuple[Any, ...]:
        if type(candidates) not in (list, tuple):
            raise TypeError("semantic provider returned a non-sequence")
        if len(candidates) > MAX_SEMANTIC_CANDIDATES:
            raise ValueError("semantic provider exceeded output limit")
        result = []
        seen: set[str] = set()
        for item in candidates:
            if not hasattr(item, "knowledge_ref") or not hasattr(
                item, "semantic_score"
            ):
                raise TypeError("semantic provider returned an invalid candidate")
            ref = item.knowledge_ref
            score = item.semantic_score
            if not isinstance(ref, str) or ref not in eligible_refs:
                raise ValueError("semantic provider returned an ineligible reference")
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
            ):
                raise ValueError("semantic provider returned an invalid score")
            # A zero-score semantic hit is not evidence.  Dropping it is what
            # makes an empty semantic result truthful for unrelated queries.
            if score <= 0:
                continue
            if ref not in seen:
                seen.add(ref)
                result.append(item)
        return tuple(
            sorted(
                result,
                key=lambda item: (-item.semantic_score, item.knowledge_ref),
            )[:MAX_SEMANTIC_CANDIDATES]
        )

    @staticmethod
    def _validated_rerank_output(output: Any, allowed_refs: set[str]) -> list[str]:
        if type(output) not in (list, tuple):
            raise TypeError("reranker returned a non-sequence")
        if len(output) > MAX_RERANK_CANDIDATES:
            raise ValueError("reranker exceeded output limit")
        refs: list[str] = []
        seen: set[str] = set()
        for item in output:
            if not isinstance(item, RerankedCandidate):
                raise TypeError("reranker returned an invalid candidate")
            if item.knowledge_ref not in allowed_refs:
                raise ValueError("reranker returned an unknown reference")
            if (
                not isinstance(item.score, (int, float))
                or isinstance(item.score, bool)
                or not math.isfinite(item.score)
            ):
                raise ValueError("reranker returned an invalid score")
            if item.knowledge_ref not in seen:
                seen.add(item.knowledge_ref)
                refs.append(item.knowledge_ref)
        return refs

    def _retime(
        self,
        result: RetrievalResult,
        started: float,
        fallback_reason: str | None = None,
    ) -> RetrievalResult:
        elapsed = max(0.0, (self.level0._monotonic() - started) * 1000)
        return replace(
            result,
            diagnostics=replace(
                result.diagnostics,
                elapsed_ms=elapsed,
                fallback_reason=fallback_reason or result.diagnostics.fallback_reason,
            ),
        )


HybridRetrievalService = Level1RetrievalService
