from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kasane.contracts import ContractViolationError
from kasane.trust import (
    AgentContext,
    Tier,
    TrustPolicyConfig,
    TrustRejectedError,
    TrustService,
)
from kasane.vault.models import EventType, KnowledgeStatus
from kasane.vault.repository import VaultRepository

REF = "vault://project/kasane/procedures/rebuild-index-on-schema-change"


def agent(session: str, actor: str = "opencode", model: str = "m1") -> AgentContext:
    return AgentContext(actor=actor, session=session, model=model)


AUTHOR = agent("author")


def proposal(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "0.1",
        "knowledge_ref": REF,
        "knowledge_type": "procedure",
        "title": "Rebuild the derived index after a schema change",
        "body": "Drop and rebuild the lexical index; never migrate it in place.",
        "tags": ["index", "migration"],
        "sources": [{"source_type": "session", "handle": "session:author"}],
    }
    value.update(overrides)
    return value


def signal(kind: str, **fields: Any) -> dict[str, Any]:
    return {"schema_version": "0.1", "knowledge_ref": REF, "kind": kind, **fields}


@pytest.fixture
def service(tmp_path: Path) -> TrustService:
    return TrustService(VaultRepository(tmp_path / "vault.db"))


def test_full_tier_lifecycle_requires_human_for_canonical(
    service: TrustService,
) -> None:
    result = service.propose(proposal(), AUTHOR)
    assert (result.record.status, result.assessment.tier) == (
        KnowledgeStatus.CANDIDATE,
        Tier.T0,
    )
    service.signal(signal("corroboration"), agent("a"))
    result = service.signal(signal("corroboration"), agent("b"))
    assert result.record.status is KnowledgeStatus.VERIFIED
    assert result.assessment.tier is Tier.T1

    service.signal(signal("outcome", outcome="success", note="worked"), agent("u1"))
    result = service.signal(
        signal("outcome", outcome="success", note="ok"), agent("u2")
    )
    assert result.assessment.tier is Tier.T2
    assert result.record.status is KnowledgeStatus.VERIFIED  # never auto-canonical
    assert [r.record.knowledge_ref for r in service.approval_queue()] == [REF]

    approved = service.approve(REF, note="looks right")
    assert approved.record.status is KnowledgeStatus.CANONICAL
    assert approved.assessment.tier is Tier.T3
    assert service.approval_queue() == []
    events = service.repository.list_events(REF)
    actors = {event.event_type: event.actor for event in events}
    assert actors[EventType.VERIFIED] == "policy"
    assert actors[EventType.PROMOTED] == "operator"


def test_independent_verification_path(service: TrustService) -> None:
    service.propose(proposal(), AUTHOR)
    result = service.signal(
        signal("verification", verification="passed", note="re-derived"), agent("v")
    )
    assert result.record.status is KnowledgeStatus.VERIFIED


def test_self_verification_rejected_without_change(service: TrustService) -> None:
    service.propose(proposal(), AUTHOR)
    before = service.repository.list_events(REF)
    with pytest.raises(TrustRejectedError, match="self-verification"):
        service.signal(signal("verification", verification="passed", note="me"), AUTHOR)
    assert service.repository.list_events(REF) == before


def test_approve_requires_t2(service: TrustService) -> None:
    service.propose(proposal(), AUTHOR)
    with pytest.raises(TrustRejectedError, match="T2"):
        service.approve(REF)


def _to_t2(service: TrustService) -> None:
    service.propose(proposal(), AUTHOR)
    service.signal(signal("verification", verification="passed", note="ok"), agent("v"))
    service.signal(signal("outcome", outcome="success", note="ok"), agent("u1"))
    service.signal(signal("outcome", outcome="success", note="ok"), agent("u2"))


def test_reject_only_applies_to_approval_queue(service: TrustService) -> None:
    service.propose(proposal(), AUTHOR)
    with pytest.raises(TrustRejectedError, match="approval queue"):
        service.reject(REF, reason="not now")
    assert service.assess(REF).record.status is KnowledgeStatus.CANDIDATE


def test_reject_archives_t2_with_reason(tmp_path: Path) -> None:
    service = TrustService(VaultRepository(tmp_path / "vault.db"))
    _to_t2(service)
    with pytest.raises(TrustRejectedError, match="reason"):
        service.reject(REF, reason=" ")
    result = service.reject(REF, reason="duplicates existing guidance")
    assert result.record.status is KnowledgeStatus.ARCHIVED
    assert service.repository.list_events(REF)[-1].actor == "operator"
    with pytest.raises(TrustRejectedError):
        service.signal(signal("corroboration"), agent("late"))


def test_reconcile_applies_policy_left_behind(service: TrustService) -> None:
    service.propose(proposal(), AUTHOR)
    record = service.repository.get_knowledge(REF)
    for session in ("a", "b"):  # simulate signals recorded before a crash
        record = service.repository.get_knowledge(REF)
        service.repository.record_signal_event(
            REF,
            EventType.CORROBORATED,
            expected_revision=record.revision,
            actor="opencode",
            metadata=agent(session).metadata(),
        )
    assert service.assess(REF).record.status is KnowledgeStatus.CANDIDATE
    assert service.reconcile() == [REF]
    assert service.assess(REF).record.status is KnowledgeStatus.VERIFIED
    assert service.reconcile() == []  # idempotent


def test_canonical_failures_revalidate_then_deprecate(service: TrustService) -> None:
    service.propose(proposal(), AUTHOR)
    service.signal(signal("verification", verification="passed", note="ok"), agent("v"))
    service.signal(signal("outcome", outcome="success", note="ok"), agent("u1"))
    service.signal(signal("outcome", outcome="success", note="ok"), agent("u2"))
    service.approve(REF)
    first = service.signal(
        signal("outcome", outcome="failure", note="broke"), agent("f1")
    )
    assert first.record.status is KnowledgeStatus.CANONICAL
    assert first.assessment.revalidation_requested
    second = service.signal(
        signal("outcome", outcome="failure", note="again"), agent("f2")
    )
    assert second.record.status is KnowledgeStatus.DEPRECATED


def test_failures_archive_candidates(service: TrustService) -> None:
    service.propose(proposal(), AUTHOR)
    service.signal(signal("outcome", outcome="failure", note="no"), agent("f1"))
    result = service.signal(
        signal("outcome", outcome="failure", note="no"), agent("f2")
    )
    assert result.record.status is KnowledgeStatus.ARCHIVED


@pytest.mark.parametrize(
    "bad",
    [
        {"sources": []},
        {"knowledge_ref": "https://example.com/x"},
        {"title": ""},
        {"unexpected": True},
        {"knowledge_type": "howto"},
        {"applies_when": {"language": "python"}},
        {"body": "x" * 4001},
    ],
)
def test_contract_violations_rejected(
    service: TrustService, bad: dict[str, Any]
) -> None:
    with pytest.raises(ContractViolationError):
        service.propose(proposal(**bad), AUTHOR)
    assert service.repository.list_knowledge() == []


def test_duplicate_scope_bytes_and_timestamp_rules(tmp_path: Path) -> None:
    service = TrustService(
        VaultRepository(tmp_path / "vault.db"),
        write_scopes=["project/kasane"],
        max_body_bytes=100,
    )
    service.propose(proposal(), AUTHOR)
    with pytest.raises(TrustRejectedError, match="already exists"):
        service.propose(proposal(), agent("other"))
    with pytest.raises(TrustRejectedError, match="write scopes"):
        service.propose(proposal(knowledge_ref="vault://global/elsewhere"), AUTHOR)
    with pytest.raises(TrustRejectedError, match="bytes"):
        service.propose(
            proposal(knowledge_ref=REF + "-2", body="あ" * 40), AUTHOR
        )  # 120 bytes
    with pytest.raises(TrustRejectedError, match="timezone"):
        service.propose(
            proposal(knowledge_ref=REF + "-3", stale_after="2027-01-01T00:00:00"),
            AUTHOR,
        )


def test_signal_contract_requires_matching_fields(service: TrustService) -> None:
    service.propose(proposal(), AUTHOR)
    with pytest.raises(ContractViolationError):
        service.signal(signal("outcome", note="missing outcome"), agent("x"))
    with pytest.raises(ContractViolationError):
        service.signal(signal("corroboration", outcome="success"), agent("x"))


def test_reserved_actor_names_refused() -> None:
    with pytest.raises(ValueError, match="reserved"):
        AgentContext(actor="operator", session="s", model="m")


def test_distinct_model_config_applies_end_to_end(tmp_path: Path) -> None:
    service = TrustService(
        VaultRepository(tmp_path / "vault.db"),
        TrustPolicyConfig(require_distinct_models=True),
    )
    service.propose(proposal(), AUTHOR)
    service.signal(signal("corroboration"), agent("a", model="m1"))
    result = service.signal(signal("corroboration"), agent("b", model="m2"))
    assert result.record.status is KnowledgeStatus.CANDIDATE
    result = service.signal(signal("corroboration"), agent("c", model="m3"))
    assert result.record.status is KnowledgeStatus.VERIFIED


def test_verified_failures_demote_to_candidate_then_archive(
    service: TrustService,
) -> None:
    def verify(session: str) -> None:
        service.signal(
            signal("verification", verification="passed", note="ok"), agent(session)
        )

    def fail(session: str) -> Any:
        return service.signal(
            signal("outcome", outcome="failure", note="did not hold"), agent(session)
        )

    service.propose(proposal(), AUTHOR)
    verify("v1")
    fail("f1")
    demoted = fail("f2")
    assert demoted.record.status is KnowledgeStatus.CANDIDATE
    assert demoted.record.verification_outcome is None
    assert demoted.assessment.failures == 0
    assert service.repository.list_events(REF)[-1].event_type is EventType.DEMOTED

    verify("v2")
    assert service.assess(REF).record.status is KnowledgeStatus.VERIFIED
    fail("f3")
    archived = fail("f4")
    assert archived.record.status is KnowledgeStatus.ARCHIVED
